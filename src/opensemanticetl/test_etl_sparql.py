#!/usr/bin/python3
# -*- coding: utf-8 -*-

import copy
import unittest
from unittest import mock

import etl_sparql


def select_response(variables, rows):
    """Build a SPARQL 1.1 JSON SELECT result for the tests."""
    return {
        "head": {"vars": variables},
        "results": {"bindings": rows},
    }


def iri(value):
    return {"type": "uri", "value": value}


def literal(value, datatype=None, lang=None):
    term = {"type": "literal", "value": value}
    if datatype:
        term["datatype"] = datatype
    if lang:
        term["xml:lang"] = lang
    return term


def bnode(value):
    return {"type": "bnode", "value": value}


class Test_select_fidelity_records(unittest.TestCase):

    def setUp(self):
        self.endpoint = "http://localhost:3030/ds/query"
        self.query = "SELECT ?s ?o WHERE { ?s ?p ?o }"

    def records(self, response, query=None):
        return etl_sparql.select_results_to_fidelity_records(
            results=response,
            endpoint=self.endpoint,
            query=query or self.query,
            bnode_scope="testscope",
        )

    def test_term_types_and_values(self):
        response = select_response(
            ["s", "o", "unbound", "empty"],
            [{
                "s": iri("http://example.org/s"),
                "o": literal("hello", lang="en"),
                "empty": literal(""),
            }],
        )

        records = self.records(response)
        self.assertEqual(len(records), 1)
        data = records[0]

        self.assertEqual(data["sparql_value_s_s"], "http://example.org/s")
        self.assertEqual(data["sparql_type_s_s"], "uri")

        self.assertEqual(data["sparql_value_o_s"], "hello")
        self.assertEqual(data["sparql_type_o_s"], "literal")
        self.assertEqual(data["sparql_lang_o_s"], "en")
        self.assertNotIn("sparql_datatype_o_s", data)

        # unbound is explicit and has no value field, an empty literal has one
        self.assertEqual(data["sparql_type_unbound_s"], "unbound")
        self.assertNotIn("sparql_value_unbound_s", data)
        self.assertEqual(data["sparql_type_empty_s"], "literal")
        self.assertEqual(data["sparql_value_empty_s"], "")

    def test_typed_literal_and_legacy_sparql10_type(self):
        response = select_response(
            ["a", "b"],
            [{
                "a": literal("42", datatype="http://www.w3.org/2001/XMLSchema#integer"),
                "b": {"type": "typed-literal", "value": "42",
                      "datatype": "http://www.w3.org/2001/XMLSchema#integer"},
            }],
        )

        data = self.records(response)[0]

        self.assertEqual(data["sparql_type_a_s"], "literal")
        self.assertEqual(
            data["sparql_datatype_a_s"],
            "http://www.w3.org/2001/XMLSchema#integer")
        self.assertEqual(data["sparql_value_a_s"], "42")
        self.assertEqual(data["sparql_type_b_s"], "literal")
        self.assertEqual(data["sparql_datatype_b_s"],
                         "http://www.w3.org/2001/XMLSchema#integer")

    def test_lexical_value_not_stripped(self):
        response = select_response(
            ["o"],
            [{"o": literal("  spaced  ")}],
        )
        data = self.records(response)[0]
        self.assertEqual(data["sparql_value_o_s"], "  spaced  ")

    def test_column_order_preserved(self):
        response = select_response(
            ["z", "a", "m"],
            [{"a": literal("1"), "z": literal("2"), "m": literal("3")}],
        )
        data = self.records(response)[0]
        self.assertEqual(
            data[etl_sparql.SPARQL_SELECT_COLUMNS_FIELD], '["z","a","m"]')

    def test_duplicate_rows_kept(self):
        row = {"o": literal("same")}
        response = select_response(["o"], [row, copy.deepcopy(row), copy.deepcopy(row)])

        records = self.records(response)
        self.assertEqual(len(records), 3)
        self.assertEqual(len(set(data["id"] for data in records)), 3)

    def test_source_column_named_id_cannot_override_primary_key(self):
        response = select_response(
            ["id", "o"],
            [{"id": literal("source-id"), "o": literal("value")}],
        )
        data = self.records(response)[0]

        self.assertEqual(data["sparql_value_id_s"], "source-id")
        self.assertNotEqual(data["id"], "source-id")
        self.assertTrue(data["id"].startswith(self.endpoint + "/sparql-select/"))

    def test_primary_key_set_independent_of_row_order(self):
        rows = [
            {"s": iri("http://example.org/1"), "o": literal("a")},
            {"s": iri("http://example.org/2"), "o": literal("b")},
            {"s": iri("http://example.org/3"), "o": literal("")},
            {"s": iri("http://example.org/4")},
        ]
        variables = ["s", "o"]

        forward = self.records(select_response(variables, rows))
        backward = self.records(select_response(variables, list(reversed(rows))))

        self.assertEqual(
            set(data["id"] for data in forward),
            set(data["id"] for data in backward))

        # identical rows keep the same multiset of keys, too
        forward_dup = self.records(select_response(variables, rows + rows))
        backward_dup = self.records(select_response(variables, list(reversed(rows + rows))))
        self.assertEqual(
            set(data["id"] for data in forward_dup),
            set(data["id"] for data in backward_dup))

    def test_blank_node_shared_within_response_only(self):
        response = select_response(
            ["a", "b"],
            [{"a": bnode("b1"), "b": bnode("b1")}],
        )

        first = self.records(response, query="SELECT 1")

        # production calls get a fresh bnode scope per response
        second = etl_sparql.select_results_to_fidelity_records(
            results=response, endpoint=self.endpoint, query="SELECT 1")

        # same label, same response: same identity
        self.assertEqual(first[0]["sparql_value_a_s"], first[0]["sparql_value_b_s"])
        self.assertEqual(first[0]["sparql_type_a_s"], "bnode")

        # different response: identity not merged
        self.assertNotEqual(
            first[0]["sparql_value_a_s"],
            second[0]["sparql_value_a_s"])
        self.assertNotEqual(first[0]["id"], second[0]["id"])

    def test_invalid_binding_raises_before_any_record(self):
        response = select_response(
            ["o"],
            [
                {"o": literal("ok")},
                {"o": {"type": "uri"}},  # no value
                {"o": literal("ok again")},
            ],
        )

        with self.assertRaises(etl_sparql.SparqlSelectBindingError):
            self.records(response)

        # other invalid shapes
        invalid_rows = [
            [{"o": {"type": "bogus", "value": "x"}}],
            [{"o": {"type": "literal", "value": 42}}],
            [{"o": {"type": "literal", "value": "x", "xml:lang": "en",
                    "datatype": "http://www.w3.org/2001/XMLSchema#string"}}],
            [{"o": literal("x", lang="en"), "unknown": literal("y")}],
        ]
        for rows in invalid_rows:
            with self.assertRaises(etl_sparql.SparqlSelectBindingError):
                self.records(select_response(["o"], rows))


class Test_index_select_fidelity(unittest.TestCase):

    def setUp(self):
        self.connector = etl_sparql.Connector_SPARQL()
        self.connector.config["plugins"] = []
        self.emitted = []
        self.emitted_parameters = []

    def fake_process(self, parameters=None, data=None):
        self.emitted.append(data)
        self.emitted_parameters.append(parameters)
        return parameters, data

    def fake_query(self, results):
        wrapper = mock.MagicMock()
        wrapper.query.return_value.convert.return_value = results
        return mock.patch.object(
            etl_sparql, 'SPARQLWrapper', return_value=wrapper)

    def test_default_select_output_unchanged(self):
        response = select_response(
            ["s", "o"],
            [
                {"s": iri("http://example.org/s"), "o": literal("hello", lang="en")},
                {"s": iri("http://example.org/s2")},
            ],
        )

        self.connector.process = self.fake_process
        endpoint = "http://endpoint"
        query = "SELECT ?s ?o WHERE { ?s ?p ?o }"

        with self.fake_query(response):
            self.connector.index_select(endpoint=endpoint, query=query)

        self.assertEqual(len(self.emitted), 2)
        self.assertEqual(self.emitted[0]['id'], endpoint + "/" + query + "/1")
        self.assertEqual(self.emitted[0]['s'], "http://example.org/s")
        self.assertEqual(self.emitted[0]['o'], "hello")
        self.assertEqual(self.emitted[1]['id'], endpoint + "/" + query + "/2")
        self.assertNotIn('o', self.emitted[1])

    def test_fidelity_mode_emits_all_rows_through_etl(self):
        response = select_response(
            ["s"],
            [{"s": iri("http://example.org/s")}],
        )

        self.connector.process = self.fake_process
        self.connector.config[etl_sparql.SPARQL_SELECT_FIDELITY_CONFIG] = True

        with self.fake_query(response):
            self.connector.index_select(
                endpoint="http://endpoint", query="SELECT ?s WHERE { ?s ?p ?o }")

        self.assertEqual(len(self.emitted), 1)
        self.assertEqual(self.emitted[0]["sparql_type_s_s"], "uri")
        # docid generated by fidelity mode is passed on to the ETL export
        self.assertEqual(self.emitted_parameters[0]['id'], self.emitted[0]['id'])

    def test_invalid_binding_emits_no_record(self):
        response = select_response(
            ["o"],
            [{"o": literal("ok")}, {"o": {"type": "uri"}}],
        )

        self.connector.process = self.fake_process
        self.connector.config[etl_sparql.SPARQL_SELECT_FIDELITY_CONFIG] = True

        with self.fake_query(response):
            with self.assertRaises(etl_sparql.SparqlSelectBindingError):
                self.connector.index_select(
                    endpoint="http://endpoint", query="SELECT 1")

        self.assertEqual(self.emitted, [])

    def test_fidelity_switch(self):
        self.assertFalse(self.connector.select_fidelity_enabled())
        self.assertTrue(self.connector.select_fidelity_enabled(fidelity=True))
        self.assertFalse(self.connector.select_fidelity_enabled(fidelity=False))

        self.connector.config[etl_sparql.SPARQL_SELECT_FIDELITY_CONFIG] = True
        self.assertTrue(self.connector.select_fidelity_enabled())
        self.assertFalse(self.connector.select_fidelity_enabled(fidelity=False))


if __name__ == '__main__':
    unittest.main()
