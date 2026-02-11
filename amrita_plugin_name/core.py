from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent

# Register your commands here
your_handle = on_command("hello")


@your_handle.handle()
async def handle_function(event: MessageEvent):
    await your_handle.finish("Hello from my first plugin!")
