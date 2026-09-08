"""会话身份边界：thread_id 不能跨演示用户访问。"""

from __future__ import annotations

import pytest

from app.api.conversations import _owned_conversation
from app.errors import ForbiddenError, NotFoundError
from app.models.agent import Conversation


@pytest.mark.db
def test_conversation_is_owned_by_request_actor(db_session):
    conversation = Conversation(thread_id="thread-owner-1", actor_id="alice")
    db_session.add(conversation)
    db_session.flush()

    assert _owned_conversation(db_session, "thread-owner-1", "alice").id == conversation.id

    with pytest.raises(ForbiddenError):
        _owned_conversation(db_session, "thread-owner-1", "bob")

    with pytest.raises(NotFoundError):
        _owned_conversation(db_session, "missing-thread", "alice")
