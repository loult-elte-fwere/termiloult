import io
import logging
from collections import defaultdict
from contextlib import contextmanager
from ctypes import CFUNCTYPE, c_char_p, c_int, cdll
from threading import Event, Lock

import numpy as np
from pyaudio import PyAudio, paContinue, paComplete
from scipy import signal
from scipy.io import wavfile
from resampy import resample

ERROR_HANDLER_FUNC = CFUNCTYPE(None, c_char_p, c_int, c_char_p, c_int, c_char_p)

def py_error_handler(filename, line, function, err, fmt):
    pass

c_error_handler = ERROR_HANDLER_FUNC(py_error_handler)

@contextmanager
def noalsaerr():
    asound = cdll.LoadLibrary('libasound.so')
    asound.snd_lib_error_set_handler(c_error_handler)
    yield
    asound.snd_lib_error_set_handler(None)


class AudioSink:
    """ Asynchronous wav player based on portaudio
    
    Only works for 16 bits little-endian mono wav. The rate defaults to 16000.
    """
    DEFAULT_SAMPLE_RATE = 16000

    player = None
    logger = logging.getLogger('AudioSink')

    # List of sound data to be mixed and played.
    _queue = None
    # The queue is used to communicate between two threads, so we need a lock.
    _queue_lock = None
    _volume = 1
    _conf_lock = None
    # Don't touch that from any other thread than the one in wich #add runs.
    _stream = None

    def __init__(self, *, volume=100):
        with noalsaerr():
            self.player = PyAudio()
        self._queue = defaultdict(list)
        self._queue_lock = Lock()
        self._conf_lock = Lock()
        self.volume = volume

    def add(self, sound, owner=None):
        """ Send sound data to be mixed with currently playing sounds """
        rate, data = wavfile.read(io.BytesIO(sound))
        if rate != self.DEFAULT_SAMPLE_RATE:
            try:
                data = resample(data.astype(np.float), rate,
                            self.DEFAULT_SAMPLE_RATE)
            except ValueError: # sometimes happen when flooding with small wav files
                return
        with self._queue_lock:
            self._queue[owner].append(data)

        if self._stream and not self._stream.is_active():
            self._stream.close()
            self._stream = None

        if not self._stream:
            self._stream = self.player.open(
                format=8,
                channels=1,
                rate=self.DEFAULT_SAMPLE_RATE,
                output=True,
                stream_callback=self._worker
            )

    @property
    def volume(self):
        """ Volume in percent """
        return 100 * self._volume

    @volume.setter
    def volume(self, value):
        assert 0 <= value <= 100
        with self._conf_lock:
            self._volume = value / 100

    def remove(self, owner):
        """ Stop playing all sounds in a category as soon as possible """
        with self._queue_lock:
            try:
                del(self._queue[owner])
            except IndexError:
                pass

    def _worker(self, in_data, frame_count, time_info, status):
        """ Function called by PyAudio each time it can play more sound """
        # The queue is also used by #add, and since this callback runs into
        # a different thread, we need to lock the queue while we process it.
        with self._queue_lock:

            if not self._queue:
                self.logger.debug('Worker\'s queue is empty')
                return b'', paComplete

            # Chunks of each sound of the size requested by portaudio
            # in frame_count. They'll be mixed together later.
            chunks = list()
            # Since we'll iterate on it, we'll avoid mutating the queue
            # and instead create a new one which will become the new queue
            # once the iteration has finished.
            new_queue = defaultdict(list)
            for owner, sounds in self._queue.items():
                for sound in sounds:
                    length = len(sound)
                    if length <= frame_count:
                        # We reached the end of a sound, so pad it if needed,
                        # as portaudio would otherwise stop working if it isn't
                        # the right size.
                        diff = frame_count - length
                        sound = np.pad(sound, (0, diff), 'constant')
                    else:
                        # We didn't reach the end of that sound, so put
                        # what's left into the new queue.
                        new_queue[owner].append(sound[frame_count:])

                    chunks.append(sound[:frame_count])

            self._queue = new_queue

        # Put all chunks into a single array and lower their volume so their 
        # sum doesn't saturate the output.
        stack = (np.stack(chunks, axis=0) // len(chunks)) * self._volume
        # Don't forget to force the int16 type else #tobytes() won't output
        # bytes with the correct format and the audio output will be garbled.
        data = np.sum(stack, axis=0, dtype=np.int16)

        return data.tobytes(), paContinue

    def close(self):
        """ Graceful shutdown
        
        Use it if you want your application to be able to exit.
        """
        self.player.terminate()
