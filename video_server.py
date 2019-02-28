#!/usr/bin/python3

##############################################################
#                                                            #
#   Copyright 2017-2018 Amazon.com, Inc. or its affiliates.  #
#   All Rights Reserved.                                     #
#                                                            #
##############################################################

""" This is a Flask server intended to server the results of the inference lambda
    and the camera's h264 stream. Only one stream is allowed to be served at a time.
    The h264 stream should not be used with a lambda using KVS.
"""
from threading import Thread, Event
import os
import stat
import ssl
import json
import logging
import logging.handlers
import queue
from flask import Flask, Response, render_template
import numpy as np
import cv2

# List of valid resolutions
RESOLUTION = {'1080p' : (1920, 1080), '720p' : (1280, 720), '480p' : (858, 480)}
# MXUVC binary used adjust the resolution and frame rate of the h264 stream.
MXUVC_BIN = "/opt/awscam/camera/installed/bin/mxuvc"
# Path to the configuration json file.
CONFIG_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'config.json')
# Path to certificate generation bash script.
CERT_GEN = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'gen_certs.sh')

def set_camera_prop(fps, resolution):
    """ Helper method that sets the cameras frame rate and resolution. Used
        predominantly by the h264 video stream, should not be called if user
        is using KVS.
        fps - Desired framerate
        resolution - Tuple of (width, height) for desired resolution, accepted
                     values in RESOLUTION.
    """
    os.system("{} --ch 1 framerate {}".format(MXUVC_BIN, fps))
    os.system("{} --ch 1 resolution {} {}".format(MXUVC_BIN, resolution[0], resolution[1]))

def invalid_key(missing_key):
    """ Helper method, returns error message for a missing key in the config.json file.
        missing_key - Key missing from the json file, intended to be used with the dictionaries
                      KeyError.
    """
    return 'Configuration file missig key: {}'.format(missing_key)

class VideoWorker(Thread):
    """ Standard video worker class, that uses openCV to attain images
        from the camera and place them in a queue to be retrieved by the
        client.
    """
    def __init__(self, video_src, config_dict, logger):
        """ video_src - Location of the video source to grab the capture from
            config_dict - Dictionary containing the amount of time in seconds to wait to
                          retrieve images from the queue (stream_timeout). The maximum
                          number of frames that can be stored in the queue (max_buffer_size).
                          Amount of time in seconds to wait to retrieve images
                          from the queue (stream_timeout).
            logger - Logger object use to log to the system.
        """
        super().__init__()
        self.video_src = video_src
        try:
            self.video_release_timeout = config_dict["video_release_timeout"]
            self.stream_timeout = config_dict['stream_timeout']
            self.img_queue = queue.Queue(maxsize=config_dict["max_buffer_size"])
        except KeyError as missing_key:
            logger.error(invalid_key(missing_key))

        self.stop_request = Event()

    def run(self):
        # Wait until a lambda constructs the results FIFO. The server can not
        # create this FIFO file because lambda and the server are run as two
        #separate  users. The server should be read only.
        while not stat.S_ISFIFO(os.stat(self.video_src).st_mode):
            continue
        video_capture = cv2.VideoCapture(self.video_src)
        while not self.stop_request.isSet():
            ret, frame = video_capture.read()
            try:
                if ret:
                    jpeg = cv2.imencode('.jpg', frame)[1]
                    self.img_queue.put_nowait(jpeg)
            except queue.Full:
                continue
        video_capture.release()

    def get_img_bytes(self):
        """ Returns the jpeg bytes from the last frame retrieved from the queue"""
        try:
            return self.img_queue.get(timeout=self.stream_timeout).tobytes()
        except queue.Empty:
            white_canvas = 255 * np.ones([RESOLUTION['480p'][0],
                                          RESOLUTION['480p'][1], 3])
            jpeg = cv2.imencode('.jpg', cv2.resize(white_canvas,
                                                   RESOLUTION['480p']))[1]
            return jpeg.tobytes()

    def join(self, timeout=None):
        self.stop_request.set()
        super().join(self.video_release_timeout)

class H264VideoWorker(VideoWorker):
    """Video worker for the H264 stream, sets the cameras h264 channels
       resolution and framerate so that the decoder can keep up with the
       server.
    """
    def __init__(self, video_src, config_dict, logger):
        """ video_src - Location of the video source to grab the capture from
            config_dict - Dictionary containing the target resolution for the served frames
                          (resolution). The target framerate (live_fps). The original resolution
                          fo the camera (original_live_resolution). The Original framerate of
                          the camera (original_live_framerate).
            logger - Logger object use to log to the system.
        """
        super().__init__(video_src, config_dict, logger)
        try:
            self.resolution = config_dict['live_resolution']
            self.orginal_resolution = config_dict['original_live_resolution']
            self.frame_rate = config_dict['live_fps']
            self.original_framerate = config_dict['original_live_framerate']
        except KeyError as missing_key:
            logger.error(invalid_key(missing_key))

        check_res = lambda resolution: 1 if resolution in RESOLUTION \
                                         else logger.error('Invalid resolution {}' \
                                                            .format(resolution))
        check_res(self.resolution)
        check_res(self.orginal_resolution)

    def run(self):
        set_camera_prop(self.frame_rate, RESOLUTION[self.resolution])
        super().run()

    def join(self, timeout=None):
        set_camera_prop(self.original_framerate, RESOLUTION[self.orginal_resolution])
        super().join()

class VideoApp(object):
    """ This is the class that manages the device video server"""
    def __init__(self):
        """ Constructor"""
        # Create logger
        self.logger = logging.getLogger('AWSVideoServer')
        self.logger.setLevel(logging.DEBUG)
        handler = logging.handlers.SysLogHandler(address='/dev/log')
        handler.setFormatter(logging.Formatter('%(name)s: <%(levelname)s> %(message)s'))
        self.logger.addHandler(handler)
        # Lambda for verifying the existence of a file.
        check_file = lambda file: 1 if os.path.isfile(file) \
                                    else self.logger.error('%s not found', file)
        # Load configuration
        check_file(CONFIG_PATH)
        with open(CONFIG_PATH) as config_file:
            self.config_dict = json.load(config_file)

        self.video_worker = None
        self.app = Flask(__name__)
        # Add the API endpoints
        self.app.add_url_rule('/', 'index', self.index, methods=['GET'])
        self.app.add_url_rule('/exit', 'exit', self.exit, methods=['GET'])
        self.app.add_url_rule('/video_feed_proj', 'video_feed_proj',
                              self.video_feed_proj, methods=['GET'])
        self.app.add_url_rule('/video_feed_live', 'video_feed_live',
                              self.video_feed_live, methods=['GET'])
       # Client authentication via self-signed certificates
        self.app.secret_key = os.urandom(12)
        context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
        context.verify_mode = ssl.CERT_REQUIRED
        try:
            check_file(CERT_GEN)
            if not os.path.isfile(self.config_dict["ca_path"]):
                os.system('bash {}'.format(CERT_GEN))

            context.load_verify_locations(self.config_dict["ca_path"])
            context.load_cert_chain(self.config_dict["server_cert_path"],
                                    self.config_dict["server_key_path"])
            self.app.run(host='0.0.0.0', port=self.config_dict["port"],
                         ssl_context=context, threaded=True, debug=False)
        except KeyError as missing_key:
            self.logger.error(invalid_key(missing_key))

    def index(self):
        """ Entry point for the client."""
        return render_template('index.html')

    def exit(self):
        """ Stops the current video worker. Intended to be called when the client
            navigated away or closes from the current tab."""
        self.stop_video_worker()
        return ('', 200)

    def video_feed_proj(self):
        """ Stops the current video worker, creates a new video worker, and begins
            generating JPEG's."""
        self.logger.info("Starting project feed")
        try:
            self.start_video_worker(self.config_dict["proj_stream_src"], True)
        except KeyError as missing_key:
            self.logger.error(invalid_key(missing_key))
        return Response(self.gen_stream(),
                        mimetype='multipart/x-mixed-replace; boundary=frame')

    def video_feed_live(self):
        """ Stops the current video worker, creates a new video worker, and begins
            generating JPEG's.
        """
        self.logger.info("Starting live feed")
        try:
            self.start_video_worker(self.config_dict["live_stream_src"], False)
        except KeyError as missing_key:
            self.logger.error(invalid_key(missing_key))
        return Response(self.gen_stream(),
                        mimetype='multipart/x-mixed-replace; boundary=frame')

    def gen_stream(self):
        """ Generates JPEG's of the desired stream."""
        while True:
            yield (b'--frame\r\n'b'Content-Type: image/jpeg\r\n\r\n'
                   + self.video_worker.get_img_bytes() + b'\r\n')

    def stop_video_worker(self):
        """ Helper method that stops the video worker"""
        if self.video_worker and self.video_worker.isAlive():
            self.video_worker.join()

    def start_video_worker(self, video_src, is_project_stream):
        """ Helper method that starts the video worker based on video source.
            video_src - Location of the video source
            is_project_stream - True if starting the project stream, False if
                                starting the live stream.
        """
        self.stop_video_worker()
        if is_project_stream:
            self.video_worker = VideoWorker(video_src, self.config_dict, self.logger)
        else:
            if not stat.S_ISFIFO(os.stat(video_src).st_mode):
                self.logger.error('Missing h264 pipe')
            self.video_worker = H264VideoWorker(video_src, self.config_dict, self.logger)

        self.video_worker.start()

if __name__ == '__main__':
    VideoApp()
