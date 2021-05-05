import argparse
import asyncio
import logging
import random
import string
import time

import math
import cv2
import numpy
from av import VideoFrame

import aiohttp

from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaPlayer, MediaRecorder

pcs = set()
channels = {}
channels["rx"] = None
channels["tx"] = None

##### Data Channel Code
def channel_log(channel, t, message):
    print("channel(%s) %s %s" % (channel.label, t, message))

def channel_send(channel, message):
    channel_log(channel, ">", message)
    channel.send(message)

time_start = None

def current_stamp():
    global time_start

    if time_start is None:
        time_start = time.time()
        return 0
    else:
        return int((time.time() - time_start) * 1000000)

##### End of Data Channel Code


##### Insert Custom Track Code
class OpenCVVideoStreamTrack(VideoStreamTrack):
    """
    A video track that returns an animated flag.
    """

    def __init__(self):
        super().__init__()  # don't forget this!
        self.counter = 0
        height, width = 480, 640
        self.vid = cv2.VideoCapture(0)

    def read_frame(self):
        # this is where we read a frame from the camera
        # this can be changed to a function that takes a frame in as a parameter, puts it in the buffer and then
        # allows that buffer to be used in the async send
        ret, frame = self.vid.read()
        if ret:
            self.last_frame = VideoFrame.from_ndarray(frame, format="bgr24")

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        self.read_frame()
        self.last_frame.pts = pts
        self.last_frame.time_base = time_base
        self.counter += 1
        return self.last_frame
##### End of Insert

def transaction_id():
    return "".join(random.choice(string.ascii_letters) for x in range(12))


class JanusPlugin:
    def __init__(self, session, url):
        self._queue = asyncio.Queue()
        self._session = session
        self._url = url

    async def send(self, payload):
        message = {"janus": "message", "transaction": transaction_id()}
        message.update(payload)
        async with self._session._http.post(self._url, json=message) as response:
            data = await response.json()
            assert data["janus"] == "ack"

        response = await self._queue.get()
        assert response["transaction"] == message["transaction"]
        return response


class JanusSession:
    def __init__(self, url):
        self._http = None
        self._poll_task = None
        self._plugins = {}
        self._root_url = url
        self._session_url = None

    async def attach(self, plugin_name: str) -> JanusPlugin:
        message = {
            "janus": "attach",
            "plugin": plugin_name,
            "transaction": transaction_id(),
        }
        async with self._http.post(self._session_url, json=message) as response:
            data = await response.json()
            assert data["janus"] == "success"
            plugin_id = data["data"]["id"]
            plugin = JanusPlugin(self, self._session_url + "/" + str(plugin_id))
            self._plugins[plugin_id] = plugin
            return plugin

    async def create(self):
        self._http = aiohttp.ClientSession()
        message = {"janus": "create", "transaction": transaction_id()}
        async with self._http.post(self._root_url, json=message) as response:
            data = await response.json()
            assert data["janus"] == "success"
            session_id = data["data"]["id"]
            self._session_url = self._root_url + "/" + str(session_id)

        self._poll_task = asyncio.ensure_future(self._poll())

    async def destroy(self):
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None

        if self._session_url:
            message = {"janus": "destroy", "transaction": transaction_id()}
            async with self._http.post(self._session_url, json=message) as response:
                data = await response.json()
                assert data["janus"] == "success"
            self._session_url = None

        if self._http:
            await self._http.close()
            self._http = None

    async def _poll(self):
        while True:
            params = {"maxev": 1, "rid": int(time.time() * 1000)}
            async with self._http.get(self._session_url, params=params) as response:
                data = await response.json()
                print(data)
                if data["janus"] == "event":
                    plugin = self._plugins.get(data["sender"], None)
                    if plugin:
                        await plugin._queue.put(data)
                    else:
                        print(data)


async def publish(plugin, player):
    """
    Send video to the room.
    """
    pc = RTCPeerConnection()
    pcs.add(pc)

    global channels
    channels["tx"] = pc.createDataChannel("JanusDataChannel")

    # configure media
    media = {"audio": False, "video": True, "data": True}
    if player and player.audio:
        pc.addTrack(player.audio)
        media["audio"] = True

    if player and player.video:
        pc.addTrack(player.video)
    else:
        pc.addTrack(OpenCVVideoStreamTrack())

    # send offer
    await pc.setLocalDescription(await pc.createOffer())
    request = {"request": "configure"}
    request.update(media)
    response = await plugin.send(
        {
            "body": request,
            "jsep": {
                "sdp": pc.localDescription.sdp,
                "trickle": False,
                "type": pc.localDescription.type,
            },
        }
    )

    # apply answer
    await pc.setRemoteDescription(
        RTCSessionDescription(
            sdp=response["jsep"]["sdp"], type=response["jsep"]["type"]
        )
    )


async def subscribe(session, room, feed, recorder, create_dc=False):
    pc = RTCPeerConnection()
    pcs.add(pc)

    if create_dc:
        channel = pc.createDataChannel("chat")
        channels["rx"] = channel

        @channel.on("open")
        async def on_open():
            print("Data Channel Opened!")

    @pc.on("track")
    async def on_track(track):
        print("Track %s received" % track.kind)
        if track.kind == "video" and recorder is not None:
            recorder.addTrack(track)
        if track.kind == "audio" and recorder is not None:
            recorder.addTrack(track)

    @pc.on('datachannel')
    async def on_datachannel(channel):
        @channel.on("message")
        async def on_message(message):
            # this is where we get data (ie. control messages) back from the server
            # these messages are basically our "remote control" events
            channel_log(channel, "0", message)

            #echo back just for fun
            global channels
            channel_send(channels["tx"], message)

    # subscribe
    plugin = await session.attach("janus.plugin.videoroom")
    response = await plugin.send(
        {"body": {"request": "join", "ptype": "subscriber", "room": room, "feed": feed}}
    )

    # apply offer
    await pc.setRemoteDescription(
        RTCSessionDescription(
            sdp=response["jsep"]["sdp"], type=response["jsep"]["type"]
        )
    )

    # send answer
    await pc.setLocalDescription(await pc.createAnswer())
    response = await plugin.send(
        {
            "body": {"request": "start"},
            "jsep": {
                "sdp": pc.localDescription.sdp,
                "trickle": False,
                "type": pc.localDescription.type,
            },
        }
    )
    if recorder is not None:
        await recorder.start()


async def run(player, recorder, room, session):

    await session.create()

    # join video room
    plugin = await session.attach("janus.plugin.videoroom")
    response = await plugin.send(
        {
            "body": {
                "display": "aiortc",
                "ptype": "publisher",
                "request": "join",
                "room": room,
            }
        }
    )
    publishers = response["plugindata"]["data"]["publishers"]
    for publisher in publishers:
        print("id: %(id)s, display: %(display)s" % publisher)

    # send video
    await publish(plugin=plugin, player=player)

    # receive video
    if publishers:
        await subscribe(
            session=session, room=room, feed=publishers[0]["id"], recorder=recorder, create_dc=True
        )

    # exchange media for 10 minutes
    print("Exchanging media")
    await asyncio.sleep(600)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Janus")
    parser.add_argument("url", help="Janus root URL, e.g. http://localhost:8088/janus")
    parser.add_argument(
        "--room",
        type=int,
        default=1234,
        help="The video room ID to join (default: 1234).",
    ),
    parser.add_argument("--play-from", help="Read the media from a file and sent it."),
    parser.add_argument("--record-to", help="Write received media to a file."),
    parser.add_argument("--verbose", "-v", action="count")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    # create signaling and peer connection
    session = JanusSession(args.url)

    # create media source
    if args.play_from and args.play_from not in ["avfoundation", "opencv"]:
        player = MediaPlayer(args.play_from)
    elif args.play_from == "avfoundation":
        player = MediaPlayer('default:none', format='avfoundation', options={'framerate': '30', 'video_size':'640x480'})
    elif args.play_from == "opencv":
        player = None

    # create media sink
    if args.record_to:
        recorder = MediaRecorder(args.record_to)
    else:
        recorder = None

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(
            run(player=player, recorder=recorder, room=args.room, session=session)
        )
    except KeyboardInterrupt:
        pass
    finally:
        if recorder is not None:
            loop.run_until_complete(recorder.stop())
        loop.run_until_complete(session.destroy())

        # close peer connections
        coros = [pc.close() for pc in pcs]
        loop.run_until_complete(asyncio.gather(*coros))
