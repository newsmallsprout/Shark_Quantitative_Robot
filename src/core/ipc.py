import zmq
import zmq.asyncio
import json
import asyncio
from typing import List, Optional

from src.runtime_obf import IPC_TOPIC_AI_SCORE
from src.utils.logger import log

class ZMQPublisher:
    """
    ZeroMQ Publisher for broadcasting messages from the AI process.
    """
    def __init__(self, port: int = 5555):
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.PUB)
        self.port = port
        
    def start(self):
        self.socket.bind(f"tcp://127.0.0.1:{self.port}")
        log.info(f"ZMQ Publisher bound to tcp://127.0.0.1:{self.port}")
        
    def publish(self, topic: str, data: dict):
        try:
            message = f"{topic} {json.dumps(data)}"
            self.socket.send_string(message)
        except Exception as e:
            log.error(f"ZMQ Publish Error: {e}")

    def close(self):
        self.socket.close()
        self.context.term()


class ZMQSubscriber:
    """
    ZeroMQ Subscriber for async consumption in the Strategy Engine.
    """
    def __init__(self, port: int = 5555, topics: Optional[List[str]] = None):
        self.context = zmq.asyncio.Context()
        self.socket = self.context.socket(zmq.SUB)
        self.port = port
        self.topics = topics if topics is not None else [IPC_TOPIC_AI_SCORE]
        self.running = False
        
    async def start(self, callback):
        self.socket.connect(f"tcp://127.0.0.1:{self.port}")
        for topic in self.topics:
            self.socket.setsockopt_string(zmq.SUBSCRIBE, topic)
            
        log.info(f"ZMQ Subscriber connected to tcp://127.0.0.1:{self.port} with topics {self.topics}")
        self.running = True
        
        while self.running:
            try:
                # Use zmq.NOBLOCK to avoid hanging if no messages, or rely on asyncio
                # zmq.asyncio sockets await gracefully
                msg = await self.socket.recv_string()
                topic, data_str = msg.split(" ", 1)
                data = json.loads(data_str)
                await callback(topic, data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"ZMQ Subscriber Error: {e}")

    def stop(self):
        self.running = False
        self.socket.close()
        self.context.term()
