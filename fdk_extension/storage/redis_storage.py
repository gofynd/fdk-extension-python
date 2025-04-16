from .base_storage import BaseStorage
from ..utilities.logger import get_logger
from typing import Union

from aioredis.client import Redis

logger = get_logger()

class RedisStorage(BaseStorage):

    def __init__(self, client: Redis, prefix_key: str=""):
        super().__init__(prefix_key)
        self.client = client

    async def get(self, key):
        logger.debug(f"RedisStorage.get: {self.prefix_key + key}")
        return await self.client.get(self.prefix_key + key)

    async def set(self, key, value):
        logger.debug(f"RedisStorage.set: {self.prefix_key + key} = {value}")
        return await self.client.set(self.prefix_key + key, value)

    async def delete(self, key):
        logger.debug(f"RedisStorage.delete: {self.prefix_key + key}")
        await self.client.delete(self.prefix_key + key)

    async def setex(self, key, ttl, value):
        logger.debug(f"RedisStorage.setex: {self.prefix_key + key} = {value}")
        return await self.client.setex(self.prefix_key + key, ttl, value)

    async def hget(self, key, hash_key):
        return await self.client.hget(self.prefix_key + key, hash_key)

    async def hset(self, key, hash_key, value):
        return await self.client.hset(self.prefix_key + key, hash_key, value)

    async def hgetall(self, key):
        return await self.client.hgetall(self.prefix_key + key)
