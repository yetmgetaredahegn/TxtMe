from channels.generic.websocket import AsyncWebsocketConsumer


class ChatRoomConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        return await super().connect()
    pass