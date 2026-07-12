package com.bobclaw.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.contentOrNull

/**
 * Client model for the U4a `GET /memory/graph` document (SPEC-UI-OVERHAUL §4 / D9).
 * The server assembles + caps; the client only renders. Node `payload` is kept as a
 * raw [JsonObject] so the inspect panel can surface whatever the substrate carries
 * (fact text / provenance / conversation turn) without pinning a fixed schema.
 */
@Serializable
data class MemoryGraph(
    val nodes: List<MemoryNode> = emptyList(),
    val edges: List<MemoryEdge> = emptyList(),
    val meta: MemoryGraphMeta = MemoryGraphMeta(),
)

@Serializable
data class MemoryNode(
    val id: String,
    val type: String,
    val label: String = "",
    val payload: JsonObject = JsonObject(emptyMap()),
) {
    private fun str(key: String): String? = payload[key]?.jsonPrimitive?.contentOrNull

    /** The fact id a `fact` node forgets by (payload.fact_id); null for non-facts. */
    val factId: String? get() = if (type == NODE_FACT) str("fact_id") else null

    /** The provenance source-conversation event id (fact → conversation edge target). */
    val sourceEventId: String? get() = str("source_event_id")

    /** Best full-text for the inspect panel, per node type. */
    val displayText: String
        get() = when (type) {
            NODE_CONVERSATION -> {
                val u = str("user_message")
                val a = str("assistant_response")
                listOfNotNull(
                    u?.takeIf { it.isNotBlank() }?.let { "You: $it" },
                    a?.takeIf { it.isNotBlank() }?.let { "Bob: $it" },
                ).joinToString("\n\n").ifBlank { label }
            }
            else -> str("text")?.takeIf { it.isNotBlank() } ?: label
        }

    /** Only `fact` nodes are forgettable (DELETE /memory/facts/{fact_id}); accept #5 fence. */
    val forgettable: Boolean get() = type == NODE_FACT && !factId.isNullOrBlank()
}

@Serializable
data class MemoryEdge(
    val source: String,
    val target: String,
    val type: String,
    val weight: Double? = null,
)

@Serializable
data class MemoryGraphMeta(
    @SerialName("node_count") val nodeCount: Int = 0,
    @SerialName("edge_count") val edgeCount: Int = 0,
    @SerialName("node_cap") val nodeCap: Int = 0,
    val truncated: Boolean = false,
    @SerialName("knn_k") val knnK: Int = 0,
    @SerialName("knn_score_floor") val knnScoreFloor: Double = 0.0,
    val collections: List<String> = emptyList(),
    @SerialName("counts_by_type") val countsByType: Map<String, Int> = emptyMap(),
    @SerialName("total_facts") val totalFacts: Int = 0,
    val warnings: List<String> = emptyList(),
)

// Node/edge type tags — mirror core/memory_graph/assembler.py.
const val NODE_FACT = "fact"
const val NODE_CONVERSATION = "conversation"
const val EDGE_PROVENANCE = "provenance"
const val EDGE_KNN = "knn"

/** A k-NN neighbour of a node, for the inspect panel's "nearest neighbours" list. */
data class MemoryNeighbor(val node: MemoryNode, val weight: Double?)

// ── Pure logic (unit-tested; no Compose, no I/O) ──────────────────────────────

private val renderJson = Json { encodeDefaults = true }

@Serializable private data class RenderNode(val id: String, val type: String, val label: String)
@Serializable private data class RenderEdge(
    val source: String,
    val target: String,
    val type: String,
    val weight: Double? = null,
)
@Serializable private data class RenderGraph(val nodes: List<RenderNode>, val edges: List<RenderEdge>)

/**
 * The distinct substrate types present, ordered for the filter bar: fact first,
 * conversation second, then any additional live collections alphabetically.
 */
fun MemoryGraph.substrateTypes(): List<String> {
    val present = LinkedHashSet<String>()
    nodes.forEach { present += it.type }
    val ordered = mutableListOf<String>()
    if (NODE_FACT in present) ordered += NODE_FACT
    if (NODE_CONVERSATION in present) ordered += NODE_CONVERSATION
    ordered += present.filter { it != NODE_FACT && it != NODE_CONVERSATION }.sorted()
    return ordered
}

/**
 * Restrict the graph to [allowed] node types (null ⇒ everything). Edges survive only
 * when BOTH endpoints survive — so the rendered graph is always internally consistent.
 */
fun MemoryGraph.filterByTypes(allowed: Set<String>?): MemoryGraph {
    if (allowed == null) return this
    val keptNodes = nodes.filter { it.type in allowed }
    val keptIds = keptNodes.mapTo(HashSet()) { it.id }
    val keptEdges = edges.filter { it.source in keptIds && it.target in keptIds }
    return copy(nodes = keptNodes, edges = keptEdges)
}

/** Compact JSON pushed to the JS renderer — id/type/label + edges only (payload stays Kotlin-side). */
fun MemoryGraph.toRenderJson(): String = renderJson.encodeToString(
    RenderGraph(
        nodes = nodes.map { RenderNode(it.id, it.type, it.label) },
        edges = edges.map { RenderEdge(it.source, it.target, it.type, it.weight) },
    )
)

/**
 * First node (list order = server priority order) whose label or full text contains
 * [query] case-insensitively. Blank query ⇒ null. Drives search-and-fly-to.
 */
fun MemoryGraph.findNode(query: String): MemoryNode? {
    val q = query.trim()
    if (q.isEmpty()) return null
    val needle = q.lowercase()
    return nodes.firstOrNull {
        it.label.lowercase().contains(needle) || it.displayText.lowercase().contains(needle)
    }
}

fun MemoryGraph.nodeById(id: String): MemoryNode? = nodes.firstOrNull { it.id == id }

/** k-NN neighbours of [id] (undirected knn edges), heaviest-similarity first. */
fun MemoryGraph.neighborsOf(id: String): List<MemoryNeighbor> {
    val byId = nodes.associateBy { it.id }
    return edges.asSequence()
        .filter { it.type == EDGE_KNN && (it.source == id || it.target == id) }
        .mapNotNull { e ->
            val otherId = if (e.source == id) e.target else e.source
            byId[otherId]?.let { MemoryNeighbor(it, e.weight) }
        }
        .sortedByDescending { it.weight ?: 0.0 }
        .toList()
}

/** The provenance source-conversation node for a fact [id] (fact → conversation edge). */
fun MemoryGraph.provenanceOf(id: String): MemoryNode? {
    val byId = nodes.associateBy { it.id }
    val edge = edges.firstOrNull { it.type == EDGE_PROVENANCE && it.source == id } ?: return null
    return byId[edge.target]
}
