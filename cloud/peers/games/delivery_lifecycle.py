"""Bidirectional signed invitations for the registered delivery game."""
from __future__ import annotations

import asyncio
import logging
import time

from .base import activity_allows, game_invitation_ready

logger = logging.getLogger(__name__)
GAME_ID = "push_delivery"
LAMP, DOG = "esp32_desk_led", "go2"


def on_invitation(messenger, message: dict, state) -> None:
    """Let the lamp independently accept, defer or decline a dog's invitation."""
    from cloud.agents.common.effects import FileEffectLedger
    from cloud.esp32.service import get_agent
    from cloud.space.context import get_space_context
    from .delivery_decisions import decide
    from .push_delivery import GAME

    activity_id = message.get("activity_id")
    if (message.get("kind") != "message"
            or message.get("interaction_kind") != "game_invite"
            or message.get("game_id") != GAME_ID
            or (message.get("sender"), message.get("target")) != (DOG, LAMP)
            or message.get("deadline", 0) <= time.time()
            or not game_invitation_ready(activity_id, DOG, LAMP)):
        messenger.send_result(message["message_id"], "rejected",
                              reason="game_participant_busy_or_expired")
        return
    agent = get_agent()
    if agent is None:
        messenger.send_result(message["message_id"], "blocked", reason="lamp_agent_unavailable")
        return
    try:
        reservation = FileEffectLedger().reserve(
            LAMP, "lamp:game:" + message["message_id"], message["message_id"],
            "accept", GAME_ID, {"activity_id": activity_id, "sender": DOG})
        if reservation["reservation"] != "new":
            messenger.send_result(message["message_id"], "unknown",
                                  reason="prior_game_execution_unconfirmed")
            return
        decision = asyncio.run(decide("lamp", "invite", {
            "game": GAME.metadata(), "message": message.get("content", ""),
            "space": get_space_context(LAMP, state=agent._state)},
            thread_id="lamp:delivery:invite:" + message["message_id"],
            deadline=message["deadline"]))
        if (time.time() >= message["deadline"]
                or not game_invitation_ready(activity_id, DOG, LAMP)):
            messenger.send_result(message["message_id"], "cancelled",
                                  reason="game_participant_busy_or_expired")
            return
        speech = str(decision.get("speech") or "").strip()[:1000]
        if not speech:
            messenger.send_result(message["message_id"], "unknown",
                                  reason="acceptance_response_unavailable")
            return
        accepted = decision.get("accepted") is True
        if not accepted:
            messenger.send_result(message["message_id"], "rejected", reason="invitation_declined")
        sent = messenger.send_message(LAMP, DOG, speech,
            reply_to=message["message_id"], reply_kind="accepted" if accepted else "declined",
            deadline=message["deadline"])
        if accepted and sent.get("result") != "accepted":
            messenger.send_result(message["message_id"], "unknown",
                                  reason="acceptance_delivery_unconfirmed")
    except Exception as exc:
        logger.exception("delivery invitation failed id=%s", message["message_id"])
        messenger.send_result(message["message_id"], "error", reason=type(exc).__name__)


def on_acceptance(messenger, message: dict, state) -> None:
    """Acknowledge lamp-origin games or queue an accepted Go2-origin game."""
    original = messenger.get_status(message.get("reply_to")) or {}
    activity_id = message.get("activity_id")
    if (message.get("reply_kind") != "accepted"
            or original.get("interaction_kind") != "game_invite"
            or original.get("game_id") != GAME_ID
            or original.get("state") not in {"waiting_start", "waiting_result"}
            or (original.get("sender"), original.get("target"))
                != (message.get("target"), message.get("sender"))
            or {message.get("sender"), message.get("target")} != {LAMP, DOG}
            or original.get("activity_id") != activity_id
            or min(message.get("deadline", 0), original.get("deadline", 0)) <= time.time()
            or not activity_allows(activity_id, LAMP, DOG)):
        messenger.send_result(message["message_id"], "rejected", reason="invalid_game_acceptance")
        return
    if original["sender"] == LAMP:
        # Handshake acknowledgement is separate from cargo discovery, physical
        # lamp color and verified delivery; no preparation action executes here.
        messenger.send_result(message["message_id"], "ok")
        return
    if not game_invitation_ready(activity_id, DOG, LAMP):
        messenger.send_result(message["message_id"], "rejected", reason="game_participant_busy")
        return
    from cloud.go2.mode.free_explore import drive
    if not drive.submit_accepted_game(message, original, messenger):
        messenger.send_result(message["message_id"], "rejected", reason="go2_autonomy_inactive")
