from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
import json
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaBlackhole
import uuid
import logging
import cv2
import asyncio
import random
import string
import os
import numpy as np
import base64
import tensorflow as tf
from tensorflow import keras
import av
import numpy as np
import uvicorn

app = FastAPI()

pcs = set()
logger = logging.getLogger("pc")

app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

# Wczytanie modelu
model = keras.models.load_model('saved_model.h5')

# Czcionka używana do napisów przez OpenCV
font = cv2.FONT_HERSHEY_COMPLEX_SMALL

# Wczytanie kaskady
face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')

origins = [
    'http://localhost',
    'http://127.0.0.1'
]

app.add_middleware(
    CORSMiddleware,
    allow_origins = ['*'],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class VideoTransformTrack(MediaStreamTrack):

    kind = "video"

    def __init__(self, track):
        super().__init__()
        self.track = track

    async def recv(self):
        
        frame = await self.track.recv()

        img = frame.to_ndarray(format="bgr24")

        processed_frame = processFrame(img)
        
        new_frame = av.VideoFrame.from_ndarray(processed_frame, format="bgr24")
        new_frame.pts = frame.pts
        new_frame.time_base = frame.time_base
        # print(new_frame)
        
        return new_frame

def processFrame(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    faces = face_cascade.detectMultiScale(gray,
                                          scaleFactor=1.1,
                                          minNeighbors=4)
    
    for (x, y, w, h) in faces:

        if (y-40) > 0:
            y0 = y-40    
        else:
            y0 = 0
        
        if (y+h+40) < frame.shape[0]:
            y1 = y+h+40  
        else:
            y1 = frame.shape[0]

        if (x-20) > 0:
            x0 = x-20
        else:
            x0 = 0

        if (x+w+20) < frame.shape[1]:
            x1 = x+w+20 
        else:
            x1 = frame.shape[1]

        img_array = keras.preprocessing.image.img_to_array(cv2.resize(cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2RGB), (140,140)))
        img_array = tf.expand_dims(img_array, 0)
        
        predictions = model.predict(img_array)
        
        score = float(predictions[0])

        predictions_text_1 = "{:.2f}% twarz z maseczka".format(100 * (1 - score))
        predictions_text_2 = "{:.2f}% twarz bez maseczki".format(100 * score)

        if score < 0.5:
            cv2.rectangle(frame, (x-20, y-40), (x+w+20, y+h+40), (0,255,0), 2)
        else:
            cv2.rectangle(frame, (x-20, y-40), (x+w+20, y+h+40), (0,0,255), 2)
        
        cv2.putText(frame, predictions_text_1, (x, y+h+50), font, 1,(0,),6,cv2.LINE_AA)
        cv2.putText(frame, predictions_text_1, (x, y+h+50), font, 1,(66,245,236),2,cv2.LINE_AA)
        cv2.putText(frame, predictions_text_2, (x, y+h+80), font, 1,(0,),6,cv2.LINE_AA)
        cv2.putText(frame, predictions_text_2, (x, y+h+80), font, 1,(66,245,236),2,cv2.LINE_AA)

    return frame

@app.get('/favicon.ico')
async def favicon():
    favicon_path = 'static/favicon.ico'
    return FileResponse(path=favicon_path)

@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    return templates.TemplateResponse("index.html", { "request": request, "id": id })

@app.post("/offer")
async def offer(request: Request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    pc_id = "PeerConnection(%s)" % uuid.uuid4()
    pcs.add(pc)

    def log_info(msg, *args):
        logger.info(pc_id + " " + msg, *args)

    recorder = MediaBlackhole()

    @pc.on("datachannel")
    def on_datachanel(channel):
        @channel.on("message")
        def on_message(message):
            if isinstance(message, str) and message.startswith("ping"):
                channel.send("pong" + message[4:])

    @pc.on("iceconnectionstatechange")
    async def on_iceconnetionstatechange():
        log_info("ICE connection state i s %s", pc.iceConnectionState)
        if pc.iceConnectionState == "failed":
            await pc.close()
            pcs.discard(pc)

    @pc.on("track")
    def on_track(track):
        log_info("Track %s received", track.kind)

        local_video = VideoTransformTrack(track)
        pc.addTrack(local_video)

        @track.on("ended")
        async def on_ended():
            log_info("Track %s ended", track.kind)
            await recorder.stop()

    # handle offer
    await pc.setRemoteDescription(offer)
    await recorder.start()

    # send answer
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return JSONResponse(content = json.dumps({
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type
    }))


@app.on_event("shutdown")
async def on_shutdown():
    # close peer connections
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()

@app.post("/photovideo")
async def image(file: UploadFile = File(...)):

    contents = await file.read()
    extension = str(file.filename).split(".")[1]

    if extension == "jpg" or extension == "png":

        nparr = np.fromstring(contents, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        processed_img = processFrame(img)

        _, encoded_img = cv2.imencode('.png', processed_img)
        encoded_file = base64.b64encode(encoded_img)

    elif extension == "mp4":

        # Zapisanie pliku do folderu z plikami tymczasowymi
        with open('tmp/'+file.filename, 'wb') as wfile:
            wfile.write(contents)

        # Odczytanie pliku tymczasowego
        cap = cv2.VideoCapture('tmp/'+file.filename)

        cap_width = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        cap_height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        cap_fps = cap.get(cv2.CAP_PROP_FPS)
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')

        out = cv2.VideoWriter('tmp/'+file.filename.split('.')[0]+'_output.mp4', fourcc, cap_fps, (int(cap_width), int(cap_height)))

        while cap.isOpened():
            ret, frame = cap.read()
            if ret == True:
                out.write(processFrame(frame))
            else:
                break
        cap.release()
        out.release()

        with open('tmp/'+file.filename.split('.')[0]+'_output.mp4', 'rb') as processed_file:
            print("Reading " + file.filename.split('.')[0]+'_output.mp4')
            output_file = processed_file.read()

        print("Encoding " + file.filename.split('.')[0]+'_output.mp4')
        encoded_file = base64.b64encode(output_file)

        os.remove('tmp/'+file.filename.split('.')[0]+'_output.mp4')
        os.remove('tmp/'+file.filename)

    return {
        'original_filename': file.filename,
        'new_filename': file.filename.split('.')[0]+'_output',
        'extension': extension,
        'encoded_file': encoded_file
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)