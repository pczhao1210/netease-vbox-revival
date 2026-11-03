import asyncio
import hashlib
import json

from .llm import LLMClient, Tool
from .playback import Alarm, Intent, Track


def tool(name, description, properties=None, required=()):
    return Tool(
        name,
        description,
        {
            "type": "object",
            "properties": properties or {},
            "required": list(required),
            "additionalProperties": False,
        },
    )


LOCAL_TOOLS = [
    tool(
        "play_music",
        "Search for and prepare music; queued does not mean audible.",
        {
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
            "enqueue": {"type": "boolean"},
        },
        ["query"],
    ),
    tool("stop_music", "Stop all playback and pending audio work."),
    tool("replay_music", "Replay the most recent track from its beginning."),
    tool("next_music", "Skip to the next queued track."),
    tool("player_state", "Read desired playback state and separate device evidence."),
    tool(
        "play_music_collection",
        "Play daily recommendations or liked songs using the authorized music account.",
        {
            "kind": {"type": "string", "enum": ["daily", "liked"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        ["kind"],
    ),
    tool(
        "set_alarm",
        "Set a daily music alarm using an authorized music search result.",
        {
            "hour": {"type": "integer", "minimum": 0, "maximum": 23},
            "minute": {"type": "integer", "minimum": 0, "maximum": 59},
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
        },
        ["hour", "minute", "query"],
    ),
    tool("list_alarms", "List daily music alarms."),
    tool(
        "delete_alarm",
        "Delete a specified alarm.",
        {"id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}},
        ["id"],
    ),
]


class Brain:
    def __init__(self, state, services, player):
        self.state = state
        self.services = services
        self.player = player
        self.task = None
        self.histories = {}
        self.epoch = 0

    async def cancel(self):
        self.epoch += 1
        epoch = self.epoch
        if self.task and self.task is not asyncio.current_task() and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        try:
            await self.services.request("mcp", "/internal/revoke", {}, timeout=2)
        except Exception:
            pass
        return epoch

    def secret(self, config, reference):
        return self.state.secret(reference, config.credential_versions.get(reference, 0))

    async def tools(self, config, allow_search=False):
        definitions = list(LOCAL_TOOLS)
        mappings = {}
        if config.search_mode == "off" or (config.search_mode == "explicit" and not allow_search):
            return definitions, mappings
        fingerprint = self.state.fingerprint(config, [config.llm.key_ref])
        probe = self.state.db.execute(
            "SELECT result FROM probes WHERE fingerprint=? AND stage=?", (fingerprint, "tools")
        ).fetchone()
        if not probe or json.loads(probe["result"]).get("status") != "passed":
            return definitions, mappings
        for server in config.mcp_servers:
            if (
                not server.enabled
                or server.server_id not in config.llm.mcp_servers
                or not server.search_tool
            ):
                continue
            if server.search_tool not in server.approved_tools:
                continue
            catalog = await self.services.request(
                "mcp",
                "/internal/discover",
                {
                    "server": server.model_dump(),
                    "key": self.secret(config, server.key_ref),
                    "private_hosts": config.approved_private_hosts,
                },
                timeout=server.timeout + 2,
            )
            for entry in catalog:
                if entry["name"] == server.search_tool and entry[
                    "hash"
                ] == server.approved_tools.get(entry["name"]):
                    definition = Tool(
                        entry["qualified_name"], entry["description"], entry["parameters"]
                    )
                    definitions.append(definition)
                    mappings[definition.name] = (server, entry["name"])
        return definitions, mappings

    async def ask(self, text, request_id, allow_search=False, speak=False):
        epoch = await self.cancel()
        if epoch != self.epoch:
            # A newer request or interrupt arrived while this one was cancelling.
            return {"text": "", "cancelled": True, "sources": []}
        self.task = asyncio.create_task(self._answer(text, request_id, allow_search, speak, epoch))
        try:
            return await self.task
        except asyncio.CancelledError:
            return {"text": "", "cancelled": True, "sources": []}

    async def _answer(self, text, request_id, allow_search, speak, epoch):
        revision, config = self.state.config()
        fingerprint = self.state.fingerprint(config, [config.llm.key_ref])
        tools, mappings = await self.tools(config, allow_search)
        sources = []

        async def execute(name, arguments, call_id):
            if epoch != self.epoch or self.state.config()[0] != revision:
                raise asyncio.CancelledError()
            operation = request_id + ":" + call_id
            if name == "player_state":
                return self.player.snapshot()
            if name == "stop_music":
                self.player.stop()
                return {"status": "stop_requested"}
            if name == "replay_music":
                return {"playback_id": self.player.replay(), "status": "queued"}
            if name == "next_music":
                self.player.next()
                return {"status": "preparing"}
            if name == "play_music_collection":
                result = await self.services.request(
                    "resolver",
                    "/collections",
                    {"kind": arguments["kind"], "limit": arguments.get("limit", 20)},
                    timeout=65,
                )
                identifiers = []
                for track in result.get("tracks", []):
                    if track.get("playable") is False:
                        continue
                    selected = Track.model_validate(
                        {key: value for key, value in track.items() if key in Track.model_fields}
                    )
                    identifiers.append(
                        self.player.submit(
                            Intent(track=selected, enqueue=bool(identifiers)),
                            operation + ":" + str(len(identifiers)),
                            source="voice",
                        )
                    )
                return {
                    "status": "preparing" if identifiers else "empty",
                    "count": len(identifiers),
                    "audible": "unknown",
                }
            if name == "list_alarms":
                return [
                    json.loads(row["document"])
                    for row in self.state.db.execute("SELECT document FROM alarms")
                ]
            if name == "delete_alarm":
                self.state.db.execute("DELETE FROM alarms WHERE id=?", (arguments["id"],))
                return {"status": "deleted", "id": arguments["id"]}
            if name == "set_alarm":
                songs = await self.services.request(
                    "resolver", "/search", {"query": arguments["query"]}, timeout=45
                )
                song = next((song for song in songs if song.get("playable", True)), None)
                if song is None:
                    return {"error": "No playable track was found"}
                track = Track.model_validate(
                    {key: value for key, value in song.items() if key in Track.model_fields}
                )
                identifier = hashlib.sha256(operation.encode()).hexdigest()[:32]
                alarm = Alarm(
                    id=identifier, hour=arguments["hour"], minute=arguments["minute"], track=track
                )
                self.state.db.execute(
                    "INSERT INTO alarms(id,document) VALUES (?,?) ON CONFLICT(id) DO NOTHING",
                    (alarm.id, alarm.model_dump_json()),
                )
                return {"status": "saved", "alarm": alarm.model_dump()}
            if name == "play_music":
                songs = await self.services.request(
                    "resolver", "/search", {"query": arguments["query"]}, timeout=45
                )
                available = [song for song in songs if song.get("playable", True)]
                if not available:
                    return {"error": "No tracks with known playback rights were found"}
                song = available[0]
                track = Track.model_validate(
                    {key: value for key, value in song.items() if key in Track.model_fields}
                )
                identifier = self.player.submit(
                    Intent(track=track, enqueue=arguments.get("enqueue", False)),
                    operation,
                    source="voice",
                )
                return {
                    "playback_id": identifier,
                    "title": track.title,
                    "status": "preparing",
                    "audible": "unknown",
                }
            if name in mappings:
                server, original_name = mappings[name]
                try:
                    result = await self.services.request(
                        "mcp",
                        "/internal/call",
                        {
                            "server": server.model_dump(),
                            "key": self.secret(config, server.key_ref),
                            "private_hosts": config.approved_private_hosts,
                            "tool": original_name,
                            "arguments": arguments,
                            "sensitive": True,
                        },
                        timeout=server.timeout + 2,
                    )
                except Exception:
                    return {
                        "error": "Search failed; there is no verified online evidence",
                        "results": [],
                    }
                if epoch != self.epoch or self.state.config()[0] != revision:
                    raise asyncio.CancelledError()
                sources.extend(result.get("results", []))
                return result
            raise ValueError("Unauthorized tool")

        client = LLMClient(
            config.llm, self.secret(config, config.llm.key_ref), config.approved_private_hosts
        )
        try:
            history = self.histories.get(fingerprint, [])
            if len(json.dumps(history)) > 64000:
                history = []
            result = await client.run(text, tools, execute, history=history)
        finally:
            await client.close()
        if epoch != self.epoch or self.state.config()[0] != revision:
            raise asyncio.CancelledError()
        self.histories = {fingerprint: result["history"]}
        if speak and result["text"] and self.player.active_kind() not in ("music", "alarm"):
            try:
                self.player.submit(
                    Intent(kind="tts", text=result["text"][:4000]), request_id + ":tts", "voice"
                )
            except ValueError:
                pass
        return {
            "text": result["text"],
            "sources": sources,
            "protocol": result["protocol"],
            "usage": result["usage"],
            "revision": revision,
        }
