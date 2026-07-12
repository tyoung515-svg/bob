package com.bobclaw.network

import com.bobclaw.model.ApprovalItem
import com.bobclaw.model.ApprovalKind
import com.bobclaw.model.Build
import com.bobclaw.model.Conversation
import com.bobclaw.model.Face
import com.bobclaw.model.HealthStatus
import com.bobclaw.model.Idea
import com.bobclaw.model.MemoryGraph
import com.bobclaw.model.MessagePage
import com.bobclaw.model.ModelInfo
import com.bobclaw.model.Project
import com.bobclaw.model.ProjectSummary
import com.bobclaw.model.BackendPalette
import com.bobclaw.model.Capabilities
import com.bobclaw.model.ChatTurn
import com.bobclaw.model.RefineResult
import com.bobclaw.model.RoutingView
import com.bobclaw.model.Team
import com.bobclaw.model.TeamDraft
import com.bobclaw.model.TeamSlot
import com.bobclaw.model.TokenPair
import io.ktor.client.HttpClient
import io.ktor.client.call.body
import io.ktor.client.plugins.ClientRequestException
import io.ktor.client.statement.bodyAsText
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.client.request.bearerAuth
import io.ktor.client.request.delete
import io.ktor.client.request.get
import io.ktor.client.request.parameter
import io.ktor.client.request.post
import io.ktor.client.request.setBody
import io.ktor.http.ContentType
import io.ktor.http.HttpStatusCode
import io.ktor.http.contentType
import io.ktor.serialization.kotlinx.json.json
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject

class RestClient(baseUrl: String) {
    private val baseUrl = baseUrl.trimEnd('/')
    private val json = Json {
        ignoreUnknownKeys = true
        isLenient = true
        encodeDefaults = true
    }

    private val httpClient = HttpClient {
        install(ContentNegotiation) {
            json(json)
        }
        // Throw on non-2xx so a 401 surfaces as a ClientRequestException — gives login a clean
        // error AND drives the 401 -> refresh retry in withAuthorizedRetry. Without this, ktor
        // tries to parse the error body as the expected type (e.g. TokenPair) and fails with a
        // confusing kotlinx "fields ... are required" MissingFieldException.
        expectSuccess = true
    }

    private var tokenPair: TokenPair? = null
    private val refreshMutex = Mutex()

    fun updateTokens(tokens: TokenPair?) {
        tokenPair = tokens
    }

    fun currentTokens(): TokenPair? = tokenPair

    /** The configured gateway base URL (trailing slash trimmed) — read-only, for the U10
     *  Settings → Connections pane. This is exactly the URL every call above is issued against. */
    fun gatewayBaseUrl(): String = baseUrl

    suspend fun login(password: String, totpCode: String?): TokenPair {
        val tokenPair = try {
            httpClient.post("$baseUrl/auth/login") {
                contentType(ContentType.Application.Json)
                setBody(LoginRequest(password = password, totpCode = totpCode))
            }.body<TokenPair>()
        } catch (e: ClientRequestException) {
            throw IllegalStateException(loginErrorMessage(e.response.status.value))
        }

        updateTokens(tokenPair)
        return tokenPair
    }

    private fun loginErrorMessage(status: Int): String = when (status) {
        401 -> "Login rejected (401): wrong password or TOTP code. Each TOTP code works once and " +
            "expires within 30s — enter the current code and try again."
        else -> "Login failed (HTTP $status)."
    }

    suspend fun refreshToken(refreshToken: String): TokenPair {
        val tokenPair = httpClient.post("$baseUrl/auth/refresh") {
            contentType(ContentType.Application.Json)
            setBody(RefreshRequest(refreshToken = refreshToken))
        }.body<TokenPair>()

        updateTokens(tokenPair)
        return tokenPair
    }

    suspend fun getConversations(limit: Int, offset: Int): List<Conversation> =
        withAuthorizedRetry { accessToken ->
            httpClient.get("$baseUrl/conversations") {
                bearerAuth(accessToken)
                parameter("limit", limit)
                parameter("offset", offset)
            }.body<ConversationListResponse>().items
        }

    suspend fun createConversation(title: String?, faceId: String?, projectId: String? = null): Conversation =
        withAuthorizedRetry { accessToken ->
            httpClient.post("$baseUrl/conversations") {
                bearerAuth(accessToken)
                contentType(ContentType.Application.Json)
                setBody(CreateConversationRequest(title = title, faceId = faceId, projectId = projectId))
            }.body()
        }

    // ---- Projects (server-side workspaces) -------------------------------------------------

    // GET /projects → {items:[ProjectSummary], limit, offset}; list items omit instructions (light).
    suspend fun getProjects(): List<ProjectSummary> =
        withAuthorizedRetry { accessToken ->
            httpClient.get("$baseUrl/projects") {
                bearerAuth(accessToken)
            }.body<ProjectListResponse>().items
        }

    // GET /projects/{id} → full project (includes instructions). Used to prefill the edit dialog.
    suspend fun getProject(id: String): Project =
        withAuthorizedRetry { accessToken ->
            httpClient.get("$baseUrl/projects/$id") {
                bearerAuth(accessToken)
            }.body()
        }

    suspend fun createProject(
        name: String,
        description: String?,
        instructions: String?,
        defaultFaceId: String?,
        defaultBackend: String?,
    ): Project =
        withAuthorizedRetry { accessToken ->
            httpClient.post("$baseUrl/projects") {
                bearerAuth(accessToken)
                contentType(ContentType.Application.Json)
                setBody(ProjectRequest(
                    name = name,
                    description = description,
                    instructions = instructions,
                    defaultFaceId = defaultFaceId,
                    defaultBackend = defaultBackend,
                ))
            }.body()
        }

    // POST /projects/{id} (update). Because Json uses encodeDefaults=true we send the FULL project
    // (all five fields) — callers must pass edited-or-current values so unchanged fields persist.
    suspend fun updateProject(
        id: String,
        name: String,
        description: String?,
        instructions: String?,
        defaultFaceId: String?,
        defaultBackend: String?,
    ): Project =
        withAuthorizedRetry { accessToken ->
            httpClient.post("$baseUrl/projects/$id") {
                bearerAuth(accessToken)
                contentType(ContentType.Application.Json)
                setBody(ProjectRequest(
                    name = name,
                    description = description,
                    instructions = instructions,
                    defaultFaceId = defaultFaceId,
                    defaultBackend = defaultBackend,
                ))
            }.body()
        }

    // DELETE /projects/{id} → archives it AND unassigns member conversations. {status, project_id} — no parse.
    suspend fun deleteProject(id: String) =
        withAuthorizedRetry { accessToken ->
            httpClient.delete("$baseUrl/projects/$id") {
                bearerAuth(accessToken)
            }
        }

    // POST /conversations/{convId}/project — projectId null/"" unassigns. Returns the updated conversation.
    suspend fun assignConversationToProject(convId: String, projectId: String?): Conversation =
        withAuthorizedRetry { accessToken ->
            httpClient.post("$baseUrl/conversations/$convId/project") {
                bearerAuth(accessToken)
                contentType(ContentType.Application.Json)
                setBody(AssignProjectRequest(projectId = projectId))
            }.body()
        }

    suspend fun getMessages(convId: String, limit: Int, before: String?): MessagePage =
        withAuthorizedRetry { accessToken ->
            httpClient.get("$baseUrl/conversations/$convId/messages") {
                bearerAuth(accessToken)
                parameter("limit", limit)
                before?.let { parameter("before", it) }
            }.body()
        }

    // Gateway 400s on an empty title — callers must guard against blank before invoking.
    suspend fun renameConversation(convId: String, title: String): Conversation =
        withAuthorizedRetry { accessToken ->
            httpClient.post("$baseUrl/conversations/$convId/rename") {
                bearerAuth(accessToken)
                contentType(ContentType.Application.Json)
                setBody(RenameConversationRequest(title = title))
            }.body()
        }

    // Soft-archive: archived conversations drop out of getConversations.
    // Response is {status, conversation_id} — no body parse needed.
    suspend fun archiveConversation(convId: String) =
        withAuthorizedRetry { accessToken ->
            httpClient.delete("$baseUrl/conversations/$convId") {
                bearerAuth(accessToken)
            }
        }

    suspend fun getFaces(): List<Face> =
        withAuthorizedRetry { accessToken ->
            httpClient.get("$baseUrl/faces") {
                bearerAuth(accessToken)
            }.body()
        }

    // GET /capabilities (gateway aggregate, MS8-G1) — the live registry the chat `/` palette lists
    // (faces / backends / capabilities), read-only. JWT-gated like every other route, so it rides
    // withAuthorizedRetry. The endpoint degrades a partial core outage to a 200 + `warnings`; a total
    // outage surfaces 502 (→ ClientRequestException here, callers fail-soft to a null document).
    suspend fun getCapabilities(): Capabilities =
        withAuthorizedRetry { accessToken ->
            httpClient.get("$baseUrl/capabilities") {
                bearerAuth(accessToken)
            }.body()
        }

    // GET /routing-view (gateway proxy → core JOAT v0). Read-only. `team` previews a
    // specific built-in fleet without changing the process default. Returns the live
    // faces → role → resolved-backend map + active_team + teams + live_probe.
    suspend fun getRoutingView(team: String? = null): RoutingView =
        withAuthorizedRetry { accessToken ->
            httpClient.get("$baseUrl/routing-view") {
                bearerAuth(accessToken)
                if (team != null) parameter("team", team)
            }.body()
        }

    // ---- JOAT teams (team-builder, DESIGN §6.4) --------------------------------

    suspend fun getTeams(): List<Team> =
        withAuthorizedRetry { accessToken ->
            httpClient.get("$baseUrl/teams") {
                bearerAuth(accessToken)
            }.body<TeamListResponse>().items
        }

    suspend fun getBackends(): BackendPalette =
        withAuthorizedRetry { accessToken ->
            httpClient.get("$baseUrl/backends") {
                bearerAuth(accessToken)
            }.body()
        }

    // POST /teams. A validation failure (400) surfaces as IllegalStateException
    // carrying the core's human message (parsed out of the error body) so the
    // builder can show "team already exists" / "not a known backend" verbatim.
    suspend fun createTeam(name: String, roles: Map<String, List<TeamSlot>>): Team =
        withAuthorizedRetry { accessToken ->
            try {
                httpClient.post("$baseUrl/teams") {
                    bearerAuth(accessToken)
                    contentType(ContentType.Application.Json)
                    setBody(CreateTeamRequest(name = name, roles = roles))
                }.body()
            } catch (e: ClientRequestException) {
                if (e.response.status == HttpStatusCode.Unauthorized) throw e  // let retry handle
                val body = runCatching { e.response.bodyAsText() }.getOrNull().orEmpty()
                val msg = Regex("\"message\"\\s*:\\s*\"([^\"]*)\"").find(body)?.groupValues?.get(1)
                throw IllegalStateException(
                    msg?.takeIf { it.isNotBlank() }
                        ?: "Create failed (HTTP ${e.response.status.value})"
                )
            }
        }

    suspend fun deleteTeam(name: String) =
        withAuthorizedRetry { accessToken ->
            httpClient.delete("$baseUrl/teams/$name") {
                bearerAuth(accessToken)
            }
        }

    // POST /teams/refine → one multi-turn refine round. The client threads the prior
    // chat (history) + the working draft; returns the assistant's reply + updated draft.
    // Never persists — the builder mirrors the draft for review + an explicit Save.
    suspend fun refineTeam(message: String, history: List<ChatTurn>, draft: TeamDraft?): RefineResult =
        withAuthorizedRetry { accessToken ->
            httpClient.post("$baseUrl/teams/refine") {
                bearerAuth(accessToken)
                contentType(ContentType.Application.Json)
                setBody(RefineRequest(message = message, history = history, draft = draft))
            }.body()
        }

    // ---- Profiles (superset of teams: roster + role prompts + shape + bounds) ----

    suspend fun getProfiles(): List<Team> =
        withAuthorizedRetry { accessToken ->
            httpClient.get("$baseUrl/profiles") {
                bearerAuth(accessToken)
            }.body<TeamListResponse>().items
        }

    // The draft IS the profile envelope ({name, roles, shape?, protocol_bounds?}). [overwrite] REPLACES
    // an existing custom profile of the same name (edit-a-team path): there is NO in-place update
    // endpoint, and core POST /profiles errors "profile already exists" unless the body carries
    // overwrite:true (core reads `overwrite=bool(body.get("overwrite"))`; the gateway forwards the body
    // verbatim). overwrite:false is byte-compatible with the create path (core strips the key on create).
    suspend fun createProfile(draft: TeamDraft, overwrite: Boolean = false): Team =
        withAuthorizedRetry { accessToken ->
            try {
                httpClient.post("$baseUrl/profiles") {
                    bearerAuth(accessToken)
                    contentType(ContentType.Application.Json)
                    setBody(profileBody(draft, overwrite))
                }.body()
            } catch (e: ClientRequestException) {
                if (e.response.status == HttpStatusCode.Unauthorized) throw e
                val body = runCatching { e.response.bodyAsText() }.getOrNull().orEmpty()
                val msg = Regex("\"message\"\\s*:\\s*\"([^\"]*)\"").find(body)?.groupValues?.get(1)
                throw IllegalStateException(
                    msg?.takeIf { it.isNotBlank() } ?: "Save failed (HTTP ${e.response.status.value})"
                )
            }
        }

    suspend fun deleteProfile(name: String) =
        withAuthorizedRetry { accessToken ->
            httpClient.delete("$baseUrl/profiles/$name") {
                bearerAuth(accessToken)
            }
        }

    // The profile POST body = the draft envelope + an `overwrite` flag (edit-a-team replace path).
    // Built by merging the serialized draft with the flag so the envelope shape stays the single
    // source of truth (no parallel request class to drift). overwrite:false ≈ the old create body.
    private fun profileBody(draft: TeamDraft, overwrite: Boolean): JsonObject {
        val base = json.encodeToJsonElement(TeamDraft.serializer(), draft).jsonObject
        return JsonObject(base + ("overwrite" to JsonPrimitive(overwrite)))
    }

    suspend fun getModels(): List<ModelInfo> =
        withAuthorizedRetry { accessToken ->
            httpClient.get("$baseUrl/models") {
                bearerAuth(accessToken)
            }.body()
        }

    suspend fun getBuilds(): List<Build> =
        withAuthorizedRetry { accessToken ->
            httpClient.get("$baseUrl/builds") {
                bearerAuth(accessToken)
            }.body()
        }

    // Gateway returns an OBJECT {status, service?, services:{name -> url}}, not an array.
    // Unauthenticated. Mapped into per-row HealthStatus so BackendHealthTile renders unchanged.
    suspend fun getHealth(): List<HealthStatus> {
        val resp: HealthResponse = httpClient.get("$baseUrl/health").body()
        return resp.services.map { (name, url) ->
            HealthStatus(name = name, status = resp.status, message = url)
        }
    }

    suspend fun getApprovals(): List<ApprovalItem> =
        withAuthorizedRetry { accessToken ->
            httpClient.get("$baseUrl/approvals") {
                bearerAuth(accessToken)
            }.body<ApprovalListResponse>().items
        }

    // GET /approvals/{id} — a single approval's freshest record (U6 detail view). Additive: the list
    // already carries the full ApprovalItem, so this only refreshes one item's latest state on expand.
    suspend fun getApproval(approvalId: String): ApprovalItem =
        withAuthorizedRetry { accessToken ->
            httpClient.get("$baseUrl/approvals/$approvalId") {
                bearerAuth(accessToken)
            }.body()
        }

    // GET /approvals/kinds — the read-only kind→metadata map (label / description / proposal_only).
    // U6 display-only enrichment: friendly labels + a "proposal — never auto-applies" badge. Static
    // on the server (needs no DB), so it stays available during a Postgres outage → fail-soft to [].
    suspend fun getApprovalKinds(): List<ApprovalKind> =
        withAuthorizedRetry { accessToken ->
            httpClient.get("$baseUrl/approvals/kinds") {
                bearerAuth(accessToken)
            }.body<ApprovalKindsResponse>().kinds
        }

    suspend fun postApprovalDecision(approvalId: String, decision: String) =
        withAuthorizedRetry { accessToken ->
            httpClient.post("$baseUrl/approvals/$approvalId/decide") {
                bearerAuth(accessToken)
                contentType(ContentType.Application.Json)
                setBody(ApprovalDecisionRequest(decision = decision))
            }
        }

    suspend fun getIdeas(state: String? = null, limit: Int = 50): List<Idea> =
        withAuthorizedRetry { accessToken ->
            val resp: IdeaListResponse = httpClient.get("$baseUrl/ideas") {
                bearerAuth(accessToken)
                parameter("limit", limit)
                if (state != null) parameter("state", state)
            }.body()
            resp.items
        }

    suspend fun createIdea(body: String, tags: List<String>): Idea =
        withAuthorizedRetry { accessToken ->
            httpClient.post("$baseUrl/ideas") {
                bearerAuth(accessToken)
                contentType(ContentType.Application.Json)
                setBody(CreateIdeaRequest(body = body, tags = tags))
            }.body()
        }

    suspend fun archiveIdea(ideaId: String) =
        withAuthorizedRetry { accessToken ->
            httpClient.delete("$baseUrl/ideas/$ideaId") {
                bearerAuth(accessToken)
            }
        }

    // ---- Memory graph (U4a `GET /memory/graph`) + Forget (existing DELETE op) ----

    // Read-only 3D-graph assembly of the internal memory substrate (L0/L1/Qdrant), SPEC §4/D9.
    // Server assembles + caps; params clamp server-side. JWT-gated → withAuthorizedRetry.
    suspend fun getMemoryGraph(
        nodes: Int? = null,
        k: Int? = null,
        floor: Double? = null,
        types: String? = null,
    ): MemoryGraph =
        withAuthorizedRetry { accessToken ->
            httpClient.get("$baseUrl/memory/graph") {
                bearerAuth(accessToken)
                nodes?.let { parameter("nodes", it) }
                k?.let { parameter("k", it) }
                floor?.let { parameter("floor", it) }
                types?.let { parameter("types", it) }
            }.body()
        }

    // Forget a fact — the EXISTING gateway op (DELETE /memory/facts/{id}); the Memory screen's
    // ONLY mutation (U4b fence: view + forget only). Response {status,fact_id} — no body parse.
    suspend fun forgetFact(factId: String) =
        withAuthorizedRetry { accessToken ->
            httpClient.delete("$baseUrl/memory/facts/$factId") {
                bearerAuth(accessToken)
            }
        }

    private suspend fun <T> withAuthorizedRetry(block: suspend (accessToken: String) -> T): T {
        val currentAccess = tokenPair?.access ?: error("Access token is missing. Call login first.")
        return try {
            block(currentAccess)
        } catch (e: ClientRequestException) {
            if (e.response.status != HttpStatusCode.Unauthorized) throw e
            val refreshed = try {
                refreshTokensOrThrow()
            } catch (refreshError: Exception) {
                updateTokens(null)
                throw refreshError
            }
            block(refreshed.access)
        }
    }

    private suspend fun refreshTokensOrThrow(): TokenPair = refreshMutex.withLock {
        val refresh = tokenPair?.refresh ?: error("Refresh token is missing.")
        refreshToken(refresh)
    }

    @Serializable
    private data class LoginRequest(
        val password: String,
        @SerialName("totp_code") val totpCode: String? = null
    )

    @Serializable
    private data class RefreshRequest(
        @SerialName("refresh_token") val refreshToken: String
    )

    @Serializable
    private data class CreateConversationRequest(
        val title: String? = null,
        @SerialName("face_id") val faceId: String? = null,
        @SerialName("model_preference") val modelPreference: String? = null,
        @SerialName("project_id") val projectId: String? = null
    )

    // Reused for both POST /projects (create) and POST /projects/{id} (update) — same body shape.
    @Serializable
    private data class ProjectRequest(
        val name: String,
        val description: String? = null,
        val instructions: String? = null,
        @SerialName("default_face_id") val defaultFaceId: String? = null,
        @SerialName("default_backend") val defaultBackend: String? = null
    )

    @Serializable
    private data class AssignProjectRequest(
        @SerialName("project_id") val projectId: String? = null
    )

    @Serializable
    private data class ProjectListResponse(
        val items: List<ProjectSummary>,
        val limit: Int = 0,
        val offset: Int = 0
    )

    @Serializable
    private data class RenameConversationRequest(
        val title: String
    )

    @Serializable
    private data class ConversationListResponse(
        val items: List<Conversation>,
        val limit: Int = 0,
        val offset: Int = 0
    )

    @Serializable
    private data class HealthResponse(
        val status: String,
        val service: String? = null,
        val services: Map<String, String> = emptyMap()
    )

    @Serializable
    private data class ApprovalListResponse(
        val items: List<ApprovalItem>,
        val limit: Int = 0,
        val offset: Int = 0,
        val status: String? = null
    )

    @Serializable
    private data class ApprovalDecisionRequest(
        val decision: String
    )

    @Serializable
    private data class ApprovalKindsResponse(
        val kinds: List<ApprovalKind> = emptyList()
    )

    @Serializable
    private data class IdeaListResponse(
        val items: List<Idea>,
        val limit: Int,
        val offset: Int,
    )

    @Serializable
    private data class CreateIdeaRequest(
        val body: String,
        val tags: List<String>,
    )

    @Serializable
    private data class TeamListResponse(
        val items: List<Team> = emptyList(),
    )

    @Serializable
    private data class CreateTeamRequest(
        val name: String,
        val roles: Map<String, List<TeamSlot>>,
    )

    @Serializable
    private data class RefineRequest(
        val message: String,
        val history: List<ChatTurn> = emptyList(),
        val draft: TeamDraft? = null,
    )
}
