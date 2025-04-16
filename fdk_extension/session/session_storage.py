from datetime import datetime
from typing import Text, Dict
import json

from ..extension import extension
from .session import Session
from utilities.logger import get_logger

logger = get_logger()
class SessionStorage:

    @staticmethod
    async def save_session(session: Session):
        if session.expires:
            ttl: float = (datetime.now() - session.expires).total_seconds()
            ttl = abs(round(min(ttl, 0)))
            return await extension.storage.setex(session.session_id, ttl, session.to_json())
        else:
            return await extension.storage.set(session.session_id, session.to_json())

    @staticmethod
    async def get_session(session_id: Text):
        session: Text = await extension.storage.get(session_id)
        logger.debug(f"middleware.get_session: {session_id} = {session}")
        if session:
            session: Dict = json.loads(session)
            session: Session = Session.clone_session(session)
        return session

    @staticmethod
    async def delete_session(session_id: Text):
        return await extension.storage.delete(session_id)
