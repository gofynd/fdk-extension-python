from .base_storage import BaseStorage
from typing import Union

from redis.asyncio import Redis


class RedisStorage(BaseStorage):

    def __init__(self, client: Redis, prefix_key: str=""):
        super().__init__(prefix_key)
        self.client = client

    async def get(self, key):
        return await self.client.get(self.prefix_key + key)

    async def set(self, key, value):
        return await self.client.set(self.prefix_key + key, value)

    async def delete(self, key):
        await self.client.delete(self.prefix_key + key)

    async def setex(self, key, ttl, value):
        return await self.client.setex(self.prefix_key + key, ttl, value)

    async def hget(self, key, hash_key):
        return await self.client.hget(self.prefix_key + key, hash_key)

    async def hset(self, key, hash_key, value):
        return await self.client.hset(self.prefix_key + key, hash_key, value)

    async def hgetall(self, key):
        return await self.client.hgetall(self.prefix_key + key)
