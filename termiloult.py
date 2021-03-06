import argparse
import html
import json
import logging
from re import search
from _curses import color_pair, init_pair
from asyncio import gather, CancelledError, get_event_loop
from contextlib import closing, suppress
from curses import (
    newwin, initscr, noecho, cbreak,
    nocbreak, endwin, echo
)
from curses import start_color
import curses
import locale
from curses.textpad import Textbox, rectangle
from functools import wraps
from itertools import product
from os.path import isfile
from threading import Thread, Lock

import websockets
from janus import Queue
from yaml import load

from tools.audiosink import AudioSink
from tools.userlist import UserList

argparser = argparse.ArgumentParser()
argparser.add_argument("--channel", "-c",
                       help="Loult channel on which to connect. Defaults to main channel",
                       default="",
                       type=str)
argparser.add_argument("--server", "-s",
                       help="Server on which to connect. Defaults to the 'Official' channel, loult.family",
                       default="loult.family",
                       type=str)
argparser.add_argument("--port", "-p",
                       help="Port of the websocket server",
                       default="80",
                       type=int)
argparser.add_argument("--cookie", "-ck",
                       help="Sets the user cookie",
                       default="flpe",
                       type=str)
argparser.add_argument("--config", "-cf",
                      help="A yaml file containing a either a cookie, a channel and/or a server domain",
                      default="config.yaml",
                      type=str)


def daemon_thread(method):
    """ Make a method launch itself into a daemon tread on invocation

    If the object has a "threads" property which is a list, then every
    new thread will be appended to it.

    Return the thread object that was created.
    """
    @wraps(method)
    def wrapped(self, *args, **kwargs):

        thread = Thread(target=method, args=(self, *args), kwargs=kwargs)
        thread.daemon = True
        thread.start()

        if hasattr(self, "threads") and isinstance(self.threads, list):
            self.threads.append(thread)

        return thread

    return wrapped


class Interface:
    """ View object which asynchronously prints and gets messages

        #input: an awaitable which returns what the user has entered.
        #output: a coroutine which sends data to be displayed.
    """

    def __init__(self):
        # Set up the terminal
        self.root_window = initscr()
        start_color()
        noecho()
        cbreak()
        self._init_color_pairs()
        locale.setlocale(locale.LC_ALL, '')
        self.root_window.keypad(True)
        try:
            start_color()
        except:
            pass

        # Curses' objects of course aren't thread-safe,
        # so we'll need a lock for every operation while
        # other threads are running.
        self.lock = Lock()
        # Those objects allow communication between threads
        # and coroutines, as well as between coroutines.
        self.input = Queue()
        self.output = Queue()

        self.threads = list()

        # This might cause the sound system to produce logs we
        # can't control; the solution is to let them be and then
        # draw on top of them later.
        self.sink = AudioSink()

        max_y, max_x = self.root_window.getmaxyx()
        self.max = (max_y, max_x)

        # A box to input things.
        # TODO: parametrize the hard-coded values.
        self.input_window = newwin(1, max_x - 2, max_y - 3, 1)
        rectangle(self.root_window, max_y - 4, 0, max_y - 2, max_x - 1)
        self.textbox = Textbox(self.input_window)

        # A box where to draw received messages.
        self.output_window = newwin(max_y - 5, max_x - 2, 0, 1)
        self.output_window.scrollok(True)

        # Draw what we just created.
        self.root_window.refresh()

        # Launch threads which update the interface and get the user's input
        self.get_input()
        self.add_messages()

    def _init_color_pairs(self):
        counter = 0
        for color_front, color_back in product(range(0, 8), range(0, 8)):
            if color_front != color_back :
                counter += 1
                init_pair(counter, color_front, color_back)

    def close(self):
        """ Change the terminal back to normal """
        self.root_window.keypad(False)
        echo()
        nocbreak()
        endwin()

    @daemon_thread
    def get_input(self):
        while True:
            msg = self.textbox.edit()
            self.input.sync_q.put(msg)
            self.input_window.clear()

    @daemon_thread
    def add_messages(self):
        window = self.output_window
        max_y, max_x = self.max
        while True:
            msg = self.output.sync_q.get()

            self.output.sync_q.task_done()
            if msg is None:
                break

            (nickname, message, color_pair_code, is_info) = msg
            window.scroll()
            if is_info:
                window.addstr(max_y - 6, 0, message)
            else:
                window.addstr(max_y - 6, 0, nickname + " : " + message, color_pair(color_pair_code))
            window.refresh()


class WebsocketClient:

    def __init__(self, server : str, channel : str, port : int, cookie : str, interface : Interface):
        self.server = server
        self.channel = channel
        self.port = port
        self.cookie = cookie
        self.user_list = None # type:UserList
        self.interface = interface

    async def get_messages(self) :
        while True:
            data = await self.ws.recv()
            if isinstance(data, bytes):
                #self.interface.sink.add(data)
                pass
            else:
                msg_data = json.loads(data, encoding="utf-8")
                msg_type = msg_data["type"]
                if msg_type == "userlist":
                    self.user_list = UserList(msg_data["users"])
                    logging.info(str(self.user_list))

                elif msg_type == "msg":
                    msg = html.unescape(msg_data["msg"])  # removing HTML shitty encoding
                    nickname = self.user_list.name(msg_data["userid"])
                    color_code = self.user_list.color(msg_data["userid"])
                    await self.interface.output.async_q.put((nickname, msg, color_code, False))

                elif msg_type == "connect":
                    # registering the user to the user list
                    self.user_list.add_user(msg_data["userid"], msg_data["params"])
                    msg = "Un %s sauvage est apparu!" % self.user_list.name(msg_data["userid"])
                    await self.interface.output.async_q.put((None, msg, None, True))

                elif msg_type == "disconnect":
                    # removing the user from the userlist
                    msg = "Le %s sauvage s'est enfui!" % self.user_list.name(msg_data["userid"])
                    await self.interface.output.async_q.put((None, msg, None, True))
                    self.user_list.del_user(msg_data["userid"])

    async def send_messages(self):
        while True:
            msg = await self.interface.input.async_q.get()

            self.interface.input.async_q.task_done()
            if msg is None:
                return

            if msg.startswith("/attack"):
                msg_split = msg.split()
                data = {"type": "attack", "target": msg[1],
                        "order": msg_split[2] if len(msg_split) >= 3 else None}

            elif msg.startswith("/ban") or msg.startswith("/slowban"):
                # typical example is /slowban apply Taupiqueur 1 2j
                msg_split = msg.split()
                if len(msg_split) >= 4 and search('^(\d+d)?(\d+h)?(\d+m)?(\d+s)?$', msg_split[-1]):
                    timeout = msg_split[-1]
                    if len(msg_split) == 4: # it means order hasn't been specified
                        order = 1
                    else:
                        order = msg_split[3]
                else:
                    timeout = "0s" # default timeout
                    order = msg_split[3]

                data = {"type": msg_split[0].strip("/"),
                        "state": msg_split[1],
                        "userid": self.user_list.get_userid_from_name(msg_split[2], int(order) - 1),
                        "timeout": timeout}

            else:
                data = {"lang": "fr", "msg": msg, "type": "msg"}

            await self.ws.send(json.dumps(data))

    async def listen(self):
        url = 'wss://%s:%i/socket/%s' % (self.server, self.port, self.channel)
        extra_headers = {"cookie": "id=%s" % self.cookie}
        async with websockets.connect(url, extra_headers=extra_headers) as ws:
            self.ws = ws
            tasks = gather(self.send_messages(), self.get_messages())
            try:
                await tasks
            except CancelledError:
                tasks.cancel()

if __name__ == "__main__":
    # parsing cmd line arguments
    args = argparser.parse_args()
    config_dict = {"channel" : args.channel,
                   "post" : args.port,
                   "cookie" : args.cookie,
                   "server" : args.server,
                   "port" : args.port}
    # loading values from the potential yaml config as "safely" as possible,
    # meaning, only if the config file is here and if the key exists, otherwise,
    # leaving the values from the command line untouched
    if isfile(args.config):
        with open(args.config) as yaml_config:
            yaml_config = load(yaml_config)
            for key in config_dict.keys():
                if key in yaml_config:
                    config_dict[key] = yaml_config[key]

        with closing(Interface()) as interface:
            ws_client = WebsocketClient(config_dict["server"], 
                                        config_dict["channel"],
                                        config_dict["port"],
                                        config_dict["cookie"], 
                                        interface)

            loop = get_event_loop()
            with suppress(KeyboardInterrupt):
                loop.run_until_complete(ws_client.listen())
