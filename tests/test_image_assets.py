"""Image input contract, real local storage/API and provider boundary."""

from __future__ import annotations

import base64
import io
import json
import logging

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.api.app import create_app
from backend.api.session_files.store import SessionFileError, SessionFileStore
from backend.api.session_store import session_store
from backend.api.state import WebAppState
from backend.domain import AssistantMessage, UserMessage, message_from_dict, message_to_dict
from backend.domain.images import ImageReference, image_budget
from backend.domain.input_message import InputMessage
from backend.domain.runtime_state import NodeWriter, RuntimeStateTree
from backend.planning import RuleBasedPlanner
from backend.planning.context_management import ContextManager
from backend.providers import (
    ChatCompletions,
    LLMClient,
    MessagesAdapter,
    ModelConfig,
    ModelRequestError,
    ResponsesAdapter,
)
from backend.runtime import AgentRunner
from backend.runtime.core.context.exchange import _chat_messages_from_nodes
from backend.storage.assets import MAX_IMAGE_BYTES, AssetError, ImageAssetStore
from backend.tools import ToolRegistry


def png() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (12, 8), "red").save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def app_session(tmp_path):
    state = WebAppState(tmp_path / "data")
    # Exercise the real admission queue without making a model request.
    state.turn_message_worker.close()
    state.model_config = lambda provider_name=None: ModelConfig(
        "local-only", "http://127.0.0.1:1/v1", "vision-test", supports_images=True
    )
    with TestClient(create_app(state)) as client:
        sidebar = client.post("/api/sidebar-threads", json={}).json()
        yield state, client, sidebar


def upload(client, session_id, data=None, filename="picture.png"):
    return client.post(f"/api/sessions/{session_id}/assets", files={"file": (filename, data or png(), "image/png")})


def test_upload_preview_metadata_and_draft_delete(app_session):
    state, client, sidebar = app_session
    sid = sidebar["session_id"]
    response = upload(client, sid, filename="../../picture.png")
    assert response.status_code == 201, response.text
    asset = response.json()
    assert asset["filename"] == "picture.png"
    assert (asset["width"], asset["height"], asset["mime_type"]) == (12, 8, "image/png")
    assert "storage_path" not in asset
    url = f"/api/sessions/{sid}/assets/{asset['asset_id']}"
    assert client.get(url).json() == asset
    assert client.get(url + "/content").content == png()
    assert client.head(url + "/content").status_code == 200
    assert client.head(url + "/content").content == b""
    store = ImageAssetStore(session_store(state), sid)
    assert store.path(asset["asset_id"]).is_relative_to(state.paths.session_db(sid).parent / "assets")
    assert not store.path(asset["asset_id"]).is_relative_to(state.paths.session_workspace(sid))
    assert client.delete(url).status_code == 200
    assert client.get(url).status_code == 404


@pytest.mark.parametrize(
    "data", [b"not an image", b"<svg></svg>", b"x" * (MAX_IMAGE_BYTES + 1)], ids=["invalid", "svg", "oversized"]
)
def test_invalid_images_rejected(app_session, data):
    _, client, sidebar = app_session
    assert upload(client, sidebar["session_id"], data).status_code == 422


def test_image_only_turn_queue_and_rewind_keep_references(app_session):
    state, client, sidebar = app_session
    sid = sidebar["session_id"]
    asset = upload(client, sid).json()
    aid = asset["asset_id"]
    response = client.post(
        "/api/turns",
        json={
            "session_id": sid,
            "thread_id": sidebar["thread_id"],
            "parent_id": "",
            "message": {"role": "user", "content": [{"type": "image", "asset_id": aid}]},
        },
    )
    assert response.status_code == 202, response.text
    turn = response.json()
    content = turn["data"][0][0]["content"]
    assert content[0]["text"] == ""
    assert content[1]["asset_id"] == aid
    assert "base64" not in json.dumps(turn)
    claimed = state.message_queue.claim_turn_start("image-test")
    assert claimed.envelope.message.images[0].asset_id == aid
    assert claimed.envelope.message.model_text() == ""
    assert client.delete(f"/api/sessions/{sid}/assets/{aid}").status_code == 409
    store = session_store(state)
    writer = NodeWriter(store)
    finished = writer.finalize(store.get_node(sid, turn["id"]), "success")
    rewound = store.append_turn_version(
        finished.id,
        InputMessage.from_input(
            "again", [{"source": "upload", "path": f"asset:{aid}", "display_path": "picture.png"}]
        ).to_items(),
    )
    assert rewound.user_message["content"][1]["asset_id"] == aid
    assert finished.user_message["content"][1]["asset_id"] == aid
    assert _chat_messages_from_nodes([rewound])[0].images[0].asset_id == aid
    runtime = AgentRunner(RuleBasedPlanner(), ToolRegistry()).new_runtime(
        task="again", session_id=sid, runtime_store=store
    )
    runtime.services.runtime_node_context = lambda: [rewound]
    visual = runtime.model_messages()[0].images[0]
    assert (visual.width, visual.height) == (12, 8)
    assert runtime.services.image_loader(aid).data == png()


def test_foreign_asset_and_image_count_rejected(app_session):
    state, client, sidebar = app_session
    sid = sidebar["session_id"]
    aid = upload(client, sid).json()["asset_id"]
    other = client.post("/api/sidebar-threads", json={}).json()
    assert client.get(f"/api/sessions/{other['session_id']}/assets/{aid}/content").status_code == 404
    for target, ids in [(other, [aid]), (sidebar, [f"asset_{i:032x}" for i in range(5)])]:
        response = client.post(
            "/api/turns",
            json={
                "session_id": target["session_id"],
                "thread_id": target["thread_id"],
                "parent_id": "",
                "message": {"role": "user", "content": [{"type": "image", "asset_id": image_id} for image_id in ids]},
            },
        )
        assert response.status_code == 422, response.text
    files = SessionFileStore(state.paths, sid)
    with pytest.raises(SessionFileError):
        files.normalize_references([{"source": "upload", "path": f"asset:asset_{i:032x}"} for i in range(5)])


def test_asset_integrity_check(app_session):
    state, client, sidebar = app_session
    aid = upload(client, sidebar["session_id"]).json()["asset_id"]
    assets = ImageAssetStore(session_store(state), sidebar["session_id"])
    assets.path(aid).write_bytes(b"modified")
    with pytest.raises(AssetError, match="重新上传"):
        assets.read(aid)


def test_empty_animated_and_excessive_dimensions_rejected(app_session, monkeypatch):
    _, client, sidebar = app_session
    url = f"/api/sessions/{sidebar['session_id']}/assets"
    assert client.post(url, files={"file": ("empty.png", b"", "image/png")}).status_code == 422
    frames = [Image.new("RGB", (4, 4), color) for color in ("red", "blue")]
    stream = io.BytesIO()
    frames[0].save(stream, format="PNG", save_all=True, append_images=frames[1:])
    assert upload(client, sidebar["session_id"], stream.getvalue()).status_code == 422
    monkeypatch.setattr("backend.storage.assets.MAX_IMAGE_PIXELS", 64)
    assert upload(client, sidebar["session_id"]).status_code == 422


@pytest.mark.parametrize("protocol,adapter", [("responses", ResponsesAdapter), ("messages", MessagesAdapter)])
def test_unsupported_protocol_never_silently_drops_images(protocol, adapter):
    runtime = AgentRunner(RuleBasedPlanner(), ToolRegistry()).new_runtime(task="image")
    runtime.exchange.messages = [UserMessage(images=(ImageReference("asset_" + "a" * 32),))]
    with pytest.raises(ModelRequestError, match="Chat Completions"):
        adapter(
            ModelConfig("local-only", "http://127.0.0.1:1", "vision", protocol=protocol, supports_images=True)
        ).prepare_request(runtime)


def test_provider_resolves_bytes_only_at_wire_boundary(app_session, caplog):
    state, client, sidebar = app_session
    sid = sidebar["session_id"]
    aid = upload(client, sid).json()["asset_id"]
    assets = ImageAssetStore(session_store(state), sid)
    image = assets.pin([aid])[0]
    message = UserMessage(content="What is this?", images=(image,))
    assert message_from_dict(message_to_dict(message)).images == message.images
    runtime = AgentRunner(RuleBasedPlanner(), ToolRegistry()).new_runtime(task="image")
    runtime.exchange.messages = [message]
    runtime.services.image_loader = assets.read
    config = ModelConfig("local-only", "http://127.0.0.1:1/v1", "vision-test", supports_images=True)
    encoded = base64.b64encode(png()).decode("ascii")

    class Transport:
        def post_json(self, endpoint, headers, payload, timeout_seconds, **kwargs):
            url = payload["messages"][0]["content"][1]["image_url"]["url"]
            assert url == "data:image/png;base64," + encoded
            return {"choices": [{"message": {"role": "assistant", "content": "red picture"}, "finish_reason": "stop"}]}

    with caplog.at_level(logging.INFO, logger="backend.providers.client"):
        LLMClient(config, transport=Transport()).run(runtime)
    assert encoded not in json.dumps(runtime.exchange.wire_request)
    assert encoded not in caplog.text
    assert "[OMITTED]" in json.dumps(runtime.exchange.wire_request)
    adapter = ChatCompletions(config)
    assert adapter.estimate_input_tokens([message], [], {}) >= image_budget(image)
    with pytest.raises(ModelRequestError):
        ChatCompletions(ModelConfig("local-only", "http://127.0.0.1:1/v1", "text-only")).prepare_request(runtime)


def test_repeated_compaction_preserves_historical_and_active_images(app_session):
    state, client, sidebar = app_session
    sid = sidebar["session_id"]
    aid = upload(client, sid).json()["asset_id"]
    assets = ImageAssetStore(session_store(state), sid)
    image = assets.pin([aid])[0]
    store = session_store(state)
    from backend.domain.runtime_state import RuntimeState

    root = store.ensure_root_node(sid)
    first = RuntimeState.create(
        session_id=sid,
        thread_id=sid,
        parent=root,
        user_content=[{"type": "text", "text": "red?", "status": "success"}, image.to_item()],
    )
    first.status = "success"
    second = RuntimeState.create(
        session_id=sid, thread_id=sid, parent=first, user_content="compare with earlier picture"
    )
    second.status = "success"
    tree = RuntimeStateTree([root, first, second])
    checkpoint = tree.compact(second, "image was red")
    assert checkpoint.assistant_items[0]["images"][0]["asset_id"] == aid
    tree = RuntimeStateTree([root, first, second, checkpoint])
    repeated = tree.compact(checkpoint, "still red")
    messages = _chat_messages_from_nodes([repeated])
    assert {image.asset_id for message in messages if isinstance(message, UserMessage) for image in message.images} == {
        aid
    }
    # The non-canonical runtime uses the same preservation rule.
    runtime = AgentRunner(RuleBasedPlanner(), ToolRegistry()).new_runtime(task="compare")
    runtime.state.messages = [UserMessage(content="red?", images=(image,)), AssistantMessage(content="red")]
    manager = ContextManager(ChatCompletions(ModelConfig("local-only", "http://127.0.0.1:1", "vision-test")))
    manager.compact(runtime, summarize=lambda transcript: "Image was red; asset ID is " + aid)
    assert any(isinstance(message, UserMessage) and message.images == (image,) for message in runtime.state.messages)
