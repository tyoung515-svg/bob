package com.bobclaw.model

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * Guards the U4b Memory-3D client model + pure interaction logic against the U4a
 * `GET /memory/graph` contract (core/memory_graph/assembler.py): shape parses, node
 * helpers read the substrate payloads, filters/search/neighbours/provenance behave,
 * and the JS render projection drops payload. No Compose — pure model logic.
 */
class MemoryGraphTest {

    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    // Mirrors an assembler document: 2 facts + 1 conversation + 1 foreign collection point,
    // provenance (fact→conversation) + one knn (fact↔fact) edge.
    private val doc = """
        {
          "nodes": [
            {"id":"fact:f1","type":"fact","label":"Sam likes dark themes",
             "payload":{"fact_id":"f1","text":"Sam likes dark themes","subject":"Sam","source_event_id":"e1","rank":1}},
            {"id":"fact:f2","type":"fact","label":"Sam uses uv",
             "payload":{"fact_id":"f2","text":"Sam uses uv for python","source_event_id":"e1"}},
            {"id":"conversation:e1","type":"conversation","label":"turn e1",
             "payload":{"event_id":"e1","user_message":"what themes?","assistant_response":"he likes dark"}},
            {"id":"research_forest:p1","type":"research_forest","label":"forest chunk",
             "payload":{"point_id":"p1","collection":"research_forest","text":"a forest node"}}
          ],
          "edges": [
            {"source":"fact:f1","target":"conversation:e1","type":"provenance"},
            {"source":"fact:f2","target":"conversation:e1","type":"provenance"},
            {"source":"fact:f1","target":"fact:f2","type":"knn","weight":0.82}
          ],
          "meta": {"node_count":4,"edge_count":3,"node_cap":500,"truncated":false,"knn_k":5,
                   "knn_score_floor":0.35,"collections":["research_forest"],
                   "counts_by_type":{"fact":2,"conversation":1,"research_forest":1},
                   "total_facts":2,"warnings":[]}
        }
    """.trimIndent()

    private fun graph(): MemoryGraph = json.decodeFromString(doc)

    @Test
    fun parses_nodes_edges_and_meta() {
        val g = graph()
        assertEquals(4, g.nodes.size)
        assertEquals(3, g.edges.size)
        assertEquals(4, g.meta.nodeCount)
        assertEquals(500, g.meta.nodeCap)
        assertEquals(5, g.meta.knnK)
        assertEquals(listOf("research_forest"), g.meta.collections)
        assertEquals(2, g.meta.countsByType["fact"])
        assertFalse(g.meta.truncated)
    }

    @Test
    fun node_helpers_read_substrate_payload() {
        val g = graph()
        val f1 = g.nodeById("fact:f1")!!
        assertEquals("f1", f1.factId)
        assertEquals("e1", f1.sourceEventId)
        assertTrue(f1.forgettable)
        assertTrue(f1.displayText.contains("dark themes"))

        val conv = g.nodeById("conversation:e1")!!
        assertFalse(conv.forgettable)          // fence: only facts are forgettable
        assertNull(conv.factId)
        assertTrue(conv.displayText.contains("You: what themes?"))
        assertTrue(conv.displayText.contains("Bob: he likes dark"))
    }

    @Test
    fun fact_without_fact_id_is_not_forgettable() {
        val n = MemoryNode(id = "fact:x", type = "fact", label = "orphan")
        assertFalse(n.forgettable)
        assertNull(n.factId)
    }

    @Test
    fun substrate_types_order_fact_then_conversation_then_collections() {
        assertEquals(listOf("fact", "conversation", "research_forest"), graph().substrateTypes())
    }

    @Test
    fun filter_drops_nodes_and_dangling_edges() {
        val onlyFacts = graph().filterByTypes(setOf("fact"))
        assertEquals(2, onlyFacts.nodes.size)
        assertTrue(onlyFacts.nodes.all { it.type == "fact" })
        // provenance edges point at the (dropped) conversation → gone; the fact↔fact knn survives.
        assertEquals(1, onlyFacts.edges.size)
        assertEquals("knn", onlyFacts.edges.single().type)
    }

    @Test
    fun filter_null_is_identity() {
        val g = graph()
        assertEquals(g, g.filterByTypes(null))
    }

    @Test
    fun render_json_projects_id_type_label_and_edges_but_not_payload() {
        val out = Json.parseToJsonElement(graph().toRenderJson()).jsonObject
        val nodes = out["nodes"]!!.jsonArray
        assertEquals(4, nodes.size)
        val first = nodes[0].jsonObject
        assertEquals(setOf("id", "type", "label"), first.keys)  // payload intentionally omitted
        val edges = out["edges"]!!.jsonArray
        assertEquals(3, edges.size)
    }

    @Test
    fun find_node_is_case_insensitive_over_label_and_text() {
        val g = graph()
        assertEquals("fact:f2", g.findNode("uv")?.id)          // by label
        assertEquals("fact:f1", g.findNode("DARK")?.id)        // case-insensitive
        assertEquals("fact:f2", g.findNode("for python")?.id)  // by full text
        assertNull(g.findNode(""))                             // blank → no fly
        assertNull(g.findNode("nonesuch"))
    }

    @Test
    fun neighbors_are_knn_only_sorted_by_weight() {
        val g = graph()
        val ns = g.neighborsOf("fact:f1")
        assertEquals(listOf("fact:f2"), ns.map { it.node.id })
        assertEquals(0.82, ns.single().weight)
        assertTrue(g.neighborsOf("conversation:e1").isEmpty())  // provenance is not a neighbour edge
    }

    @Test
    fun provenance_resolves_fact_to_source_conversation() {
        val g = graph()
        assertEquals("conversation:e1", g.provenanceOf("fact:f1")?.id)
        assertNull(g.provenanceOf("conversation:e1"))  // conversations have no outgoing provenance
    }
}
