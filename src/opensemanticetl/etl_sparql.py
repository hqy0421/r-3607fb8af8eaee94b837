#!/usr/bin/python3
# -*- coding: utf-8 -*-

import hashlib
import json
import os
import tempfile
import uuid

from etl import ETL
from enhance_rdf import enhance_rdf

from SPARQLWrapper import SPARQLWrapper, XML, JSON


#
# Optional fidelity mode for SPARQL SELECT results
#
# The default SELECT import flattens every bound value to a plain string and
# numbers the rows by their position in the response. That loses the RDF term
# type, the exact lexical form, the column order and makes duplicate rows and
# stable primary keys impossible to preserve.
#
# Fidelity mode (config['sparql_select_fidelity'] = True or --fidelity) instead
# writes for every column, in the order of the SELECT result:
#
#   sparql_value_<column>_s     exact lexical value
#   sparql_type_<column>_s      one of uri, literal, bnode or unbound
#   sparql_datatype_<column>_s  datatype IRI (typed literals only)
#   sparql_lang_<column>_s      language tag (language tagged literals only)
#
# The ordered column list of the SELECT result is written as a JSON array to
# sparql_select_columns_s, because a single valued string field keeps the order
# that multi valued index fields do not guarantee.
#
# Unbound columns are represented by an explicit type "unbound" and no value
# field, so they can be told apart from an empty string literal. A column named
# "id" is written to sparql_value_id_s and can never overwrite the index
# primary key in data['id'].
#
# The primary key of a row without blank nodes is a digest of its bindings, so
# importing the same rows in another response order yields the same set of
# keys. Duplicate rows get one document each, numbered per digest. Blank node
# identity is scoped to a single response, so labels are not merged across
# responses.
#

SPARQL_SELECT_FIDELITY_CONFIG = 'sparql_select_fidelity'

SPARQL_SELECT_COLUMNS_FIELD = 'sparql_select_columns_s'

SPARQL_TERM_TYPES = ('uri', 'literal', 'typed-literal', 'bnode')

SPARQL_TERM_KEYS = ('type', 'value', 'xml:lang', 'datatype')


class SparqlSelectBindingError(ValueError):
    """A SPARQL SELECT response contains an invalid binding."""


def _fidelity_enabled(value):
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


def _column_field(variable, role):
    return 'sparql_{}_{}_s'.format(role, variable)


def _normalize_term(term, row_index, variable):
    """Validate one SPARQL JSON term and return (type, value, datatype, lang)."""

    if not isinstance(term, dict):
        raise SparqlSelectBindingError(
            "Row {} column {}: binding is not an object".format(row_index, variable))

    unknown_keys = set(term) - set(SPARQL_TERM_KEYS)
    if unknown_keys:
        raise SparqlSelectBindingError(
            "Row {} column {}: unknown binding keys {}".format(
                row_index, variable, sorted(unknown_keys)))

    term_type = term.get('type')
    if term_type not in SPARQL_TERM_TYPES:
        raise SparqlSelectBindingError(
            "Row {} column {}: unknown term type {!r}".format(row_index, variable, term_type))

    if 'value' not in term or not isinstance(term['value'], str):
        raise SparqlSelectBindingError(
            "Row {} column {}: missing or non-string value".format(row_index, variable))

    value = term['value']
    lang = term.get('xml:lang')
    datatype = term.get('datatype')

    if lang is not None:
        if not isinstance(lang, str) or not lang:
            raise SparqlSelectBindingError(
                "Row {} column {}: invalid language tag".format(row_index, variable))
        if term_type != 'literal':
            raise SparqlSelectBindingError(
                "Row {} column {}: language tag on non literal term".format(row_index, variable))
        if datatype is not None:
            raise SparqlSelectBindingError(
                "Row {} column {}: language tag and datatype are mutually exclusive".format(
                    row_index, variable))

    if datatype is not None:
        if not isinstance(datatype, str) or not datatype:
            raise SparqlSelectBindingError(
                "Row {} column {}: invalid datatype IRI".format(row_index, variable))
        if term_type not in ('literal', 'typed-literal'):
            raise SparqlSelectBindingError(
                "Row {} column {}: datatype on non literal term".format(row_index, variable))

    if term_type == 'typed-literal':
        if datatype is None:
            raise SparqlSelectBindingError(
                "Row {} column {}: typed literal without datatype".format(row_index, variable))
        # SPARQL 1.0 result format uses typed-literal, SPARQL 1.1 uses literal plus datatype
        term_type = 'literal'

    return term_type, value, datatype, lang


def validate_select_results(results):
    """Validate a complete SPARQL SELECT response before anything is emitted.

    Returns the ordered column names and the normalized rows. Raises
    SparqlSelectBindingError if any binding in the response is invalid.
    """

    if not isinstance(results, dict):
        raise SparqlSelectBindingError("SPARQL result is not an object")

    head = results.get('head')
    if not isinstance(head, dict):
        raise SparqlSelectBindingError("SPARQL result has no head")

    variables = head.get('vars')
    if not isinstance(variables, list):
        raise SparqlSelectBindingError("SPARQL result has no variable list")
    for variable in variables:
        if not isinstance(variable, str) or not variable:
            raise SparqlSelectBindingError(
                "SPARQL result has an invalid variable name {!r}".format(variable))
    if len(set(variables)) != len(variables):
        raise SparqlSelectBindingError("SPARQL result has duplicate variable names")

    body = results.get('results')
    if not isinstance(body, dict):
        raise SparqlSelectBindingError("SPARQL result has no results")

    bindings = body.get('bindings')
    if not isinstance(bindings, list):
        raise SparqlSelectBindingError("SPARQL result has no bindings")

    variable_set = set(variables)
    rows = []
    for row_index, row in enumerate(bindings):

        if not isinstance(row, dict):
            raise SparqlSelectBindingError(
                "Row {} is not an object".format(row_index))

        normalized = {}
        for variable in row:
            if variable not in variable_set:
                raise SparqlSelectBindingError(
                    "Row {} binds unknown variable {!r}".format(row_index, variable))
            normalized[variable] = _normalize_term(row[variable], row_index, variable)

        rows.append(normalized)

    return variables, rows


def select_results_to_fidelity_records(results, endpoint, query, bnode_scope=None):
    """Build all fidelity records of a SELECT response or raise before any.

    The whole response is validated and converted before the caller emits
    anything, so an invalid binding in the middle of the response means no
    record of this batch is emitted.

    bnode_scope is only used for tests; production calls get a fresh random
    scope per response so blank node labels never merge across responses.
    """

    variables, rows = validate_select_results(results)

    if bnode_scope is None:
        bnode_scope = uuid.uuid4().hex
    bnode_prefix = '_:{}:'.format(bnode_scope)

    query_digest = hashlib.sha256(str(query).encode('utf-8')).hexdigest()

    occurrences = {}
    records = []

    for row in rows:

        data = {}
        canonical = []

        for variable in variables:

            if variable not in row:
                # explicit unbound marker so downstream can tell it apart from an empty literal
                canonical.append([variable, 'unbound'])
                data[_column_field(variable, 'type')] = 'unbound'
                continue

            term_type, value, datatype, lang = row[variable]

            if term_type == 'bnode':
                # blank node identity is shared within this response only
                value = bnode_prefix + value

            canonical.append([variable, term_type, value, datatype, lang])

            # exact lexical value, no stripping or type conversion
            data[_column_field(variable, 'value')] = value
            data[_column_field(variable, 'type')] = term_type
            if datatype is not None:
                data[_column_field(variable, 'datatype')] = datatype
            if lang is not None:
                data[_column_field(variable, 'lang')] = lang

        row_digest = hashlib.sha256(
            json.dumps(canonical, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        ).hexdigest()

        # identical rows are interchangeable, their set of occurrence numbers
        # does not depend on the response order
        occurrence = occurrences.get(row_digest, 0)
        occurrences[row_digest] = occurrence + 1

        data[SPARQL_SELECT_COLUMNS_FIELD] = json.dumps(
            variables, ensure_ascii=False, separators=(',', ':'))
        # set the primary key after the source columns, so no column can override it
        data['id'] = '{}/sparql-select/{}/{}/{}'.format(
            endpoint, query_digest, row_digest, occurrence)

        records.append(data)

    return records


#
# download (part of) graph by SPARQL query from SPARQL endpoint to RDF file
#

def download_rdf_from_sparql_endpoint(endpoint, query):

    # read graph by construct query results from SPARQL endpoint
    sparql = SPARQLWrapper(endpoint)
    sparql.setQuery(query)
    sparql.setReturnFormat(XML)
    results = sparql.query().convert()

    # crate temporary filename
    file = tempfile.NamedTemporaryFile()
    filename = file.name
    file.close()

    # export graph to RDF file
    results.serialize(destination=filename, format="xml")

    return filename


#
# Append values from SPARQL SELECT result to plain text list file
#

def sparql_select_to_list_file(endpoint, query, filename=None):

    # read graph by construct query results from SPARQL endpoint
    sparql = SPARQLWrapper(endpoint)
    sparql.setQuery(query)
    sparql.setReturnFormat(JSON)
    results = sparql.query().convert()

    if not filename:
        # crate temporary filename
        listfile = tempfile.NamedTemporaryFile(delete=False)
        filename = listfile.name
        listfile.close()

    listfile = open(filename, 'a', encoding="utf-8")

    for result in results["results"]["bindings"]:

        for variable in results["head"]["vars"]:
            if variable in result:
                if "value" in result[variable]:
                    value = result[variable]["value"]
                    value = value.strip()
                    if value:
                        listfile.write(result[variable]["value"] + "\n")

    listfile.close()

    return filename


class Connector_SPARQL(ETL):

    def __init__(self, verbose=False, quiet=True):

        ETL.__init__(self, verbose=verbose)

        self.read_configfiles()

        self.config["plugins"] = []

    def read_configfiles(self):
        #
        # include configs
        #

        # windows style filenames
        self.read_configfile('conf\\opensemanticsearch-connector')
        self.read_configfile('conf\\opensemanticsearch-enhancer-rdf')
        self.read_configfile('conf\\opensemanticsearch-connector-sparql')

        # linux style filenames
        self.read_configfile('/etc/opensemanticsearch/etl')
        self.read_configfile('/etc/opensemanticsearch/etl-custom')
        self.read_configfile('/etc/opensemanticsearch/enhancer-rdf')
        self.read_configfile('/etc/opensemanticsearch/connector-sparql')

    # Import RDF from SPARQL result

    def index_rdf(self, endpoint, query):

        # download (part of) graph from endpoint to temporary rdf file
        rdffilename = download_rdf_from_sparql_endpoint(
            endpoint=endpoint, query=query)

        parameters = self.config.copy()

        # import the triples of rdf graph by RDF plugin
        enhancer = enhance_rdf()
        enhancer.etl_graph_file(
            docid=endpoint, filename=rdffilename, parameters=parameters)

        os.remove(rdffilename)

    # Import fields and values from SPARQL SELECT result

    def select_fidelity_enabled(self, fidelity=None):

        if fidelity is not None:
            return _fidelity_enabled(fidelity)

        return _fidelity_enabled(self.config.get(SPARQL_SELECT_FIDELITY_CONFIG, False))

    # Import SPARQL SELECT result keeping term types, lexical values,
    # column order and duplicate rows (optional fidelity mode)
    def index_select_fidelity(self, endpoint, query, results):

        # the whole response is validated and converted before the first record
        # is emitted, so one invalid binding means no record of this batch at all
        records = select_results_to_fidelity_records(
            results=results, endpoint=endpoint, query=query)

        if self.verbose:
            print("Importing {} row(s) in SPARQL SELECT fidelity mode".format(len(records)))

        for data in records:
            # primary key explicitly as parameter, too, so exporters deriving the
            # docid from parameters use the generated key and not a source column
            self.process(parameters={'id': data['id']}, data=data)

    def index_select(self, endpoint, query, fidelity=None):

        # read graph by construct query results from SPARQL endpoint
        sparql = SPARQLWrapper(endpoint)
        sparql.setQuery(query)
        sparql.setReturnFormat(JSON)
        results = sparql.query().convert()

        if self.select_fidelity_enabled(fidelity=fidelity):
            self.index_select_fidelity(endpoint=endpoint, query=query, results=results)
            return

        i = 0
        for result in results["results"]["bindings"]:
            i += 1
            data = {}
            data['id'] = endpoint + "/" + query + "/" + str(i)

            for variable in results["head"]["vars"]:
                if variable in result:
                    if "value" in result[variable]:
                        data[variable] = result[variable]["value"]

            self.process(data=data)

    # Import SPARQL result

    def index(self, endpoint, query, fidelity=None):

        if query.startswith("SELECT "):
            self.index_select(endpoint, query, fidelity=fidelity)
        else:
            self.index_rdf(endpoint, query)


#
# If runned (not imported for functions) get parameters and start
#

if __name__ == "__main__":

    # todo: if no protocoll, use http://

    # get uri or filename from args
    from optparse import OptionParser
    parser = OptionParser("etl-sparql [options] uri query")
    parser.add_option("-v", "--verbose", dest="verbose",
                      action="store_true", default=None, help="Print debug messages")
    parser.add_option("-c", "--config", dest="config",
                      default=False, help="Config file")
    parser.add_option("-p", "--plugins", dest="plugins",
                      default=False, help="Plugins (comma separated)")
    parser.add_option("-f", "--fidelity", dest="fidelity",
                      action="store_true", default=None,
                      help="Fidelity mode for SPARQL SELECT results: keep term types, lexical values, column order and duplicate rows")

    (options, args) = parser.parse_args()

    if len(args) != 2:
        parser.error("Missing parameters endpoint URI and SPARQL query")

    connector = Connector_SPARQL()

    # add optional config parameters
    if options.config:
        connector.read_configfile(options.config)

    # set (or if config overwrite) plugin config
    if options.plugins:
        connector.config['plugins'] = options.plugins.split(',')

    if options.verbose == False or options.verbose == True:
        connector.verbose = options.verbose

    connector.index(endpoint=args[0], query=args[1], fidelity=options.fidelity)
