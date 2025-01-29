"""
DSuper8 project based on Joe Herman's rpi-film-capture.

Software modified by Manuel Ángel.

User interface redesigned by Manuel Ángel.

controlProcess.py: Control of lighting and motor movement.

Latest version: 20231130.
"""

from time import sleep
import serial
from logging import info
from threading import Lock
from multiprocessing import Process, Event, Queue, Value
import time
import datetime
import re


# Our configuration module.
import config

# Serial connection to Arduino
arduino = serial.Serial('/dev/ttyUSB0', 115200, timeout=1)
sleep(2)  # Wait for Arduino to initialize

# Management of the Arduino for lighting control and motor movement.
class DS8Control():

    # Image capture event.
    capEvent = Event()

    # Queue for sending orders to the motor control process MotorDriver.
    # The direction of rotation and the number of advance frames will be sent.
    motorQueue = Queue()

    def __init__(self, capEvent, motorQueue):

        self.capEvent = capEvent
        self.motorQueue = motorQueue

        # The motor starts. According to tests carried out, it is advisable not
        # to activate and deactivate the motor continuously. Operation is more
        # precise and smoother if the motor is permanently active.
        self.motorWake()

        # Engine status:
        # 0 -> stop
        # 1 -> advance
        # -1 -> recoil
        self.motorstate = 0

    # lightCheckbox
    def lightOn(self):
        arduino.write(b'LIGHT_ON\n')  # Send command to turn on the light

    def lightOff(self):
        arduino.write(b'LIGHT_OFF\n')  # Send command to turn off the light

    # fRevButton
    def motorRev(self):
        if self.motorstate:
            self.motorStop()
        # cb -> continuous recoil
        self.motorQueue.put(["cb", 0])
        self.capEvent.set()
        self.motorstate = -1

    # reverseButton
    def revFrame(self, num):
        if self.motorstate:
            self.motorStop()
        while self.capEvent.is_set():
            sleep(0.5)
        # b -> recoil, num -> number of frames
        self.motorQueue.put(["b", num])
        self.capEvent.set()
        self.motorstate = -1

    # stopButton
    def motorStop(self):
        # s -> motor stop
        if self.motorstate:
            self.motorQueue.put(["s", 0])
            self.motorstate = 0
        else:
            return

    # forwardButton
    def fwdFrame(self, num):
        if self.motorstate:
            self.motorStop()
        while self.capEvent.is_set():
            sleep(0.5)
        # f -> advance, num -> number of frames
        self.motorQueue.put(["f", num])
        self.capEvent.set()
        self.motorstate = 1

    # ffdButton
    def motorFwd(self):
        if self.motorstate:
            self.motorStop()
        # cf -> continuous advance
        self.motorQueue.put(["cf", 0])
        self.capEvent.set()
        self.motorstate = 1

    def cleanup(self):
        self.lightOff()
        self.motorStop()
        self.motorSleep()
        arduino.close()
        info("Serial connection closed")

    def motorWake(self):
        arduino.write(b'MOTOR_WAKE\n')  # Send command to wake the motor
        info("Motor on")
        sleep(0.5)

    def motorSleep(self):
        arduino.write(b'MOTOR_SLEEP\n')  # Send command to sleep the motor
        info("Motor off")
        sleep(0.5)




class arduinoInstance:
    ready = False
    currentFrame = 0
    varPattern = r"\{(.*?)\}"

    def __init__(self, serial_port: serial):
        self.serial_port = serial_port
        self.commandInProgress = False

    def serialProcess(self, write_value: str):
        time.sleep(0.1)
        data = None
        while self.serial_port.in_waiting > 0:
            data = self.serial_port.readline()
            if b"finished command" in data:
                print("Arduino Has Finished Executing Command - Ready For Next")
                self.commandInProgress = False
            if b"Cur Frame" in data:
                regex = re.search(self.varPattern, str(data))
                self.currentFrame = regex.group(1)
                info(
                    "Current Frame is now: {} @ {}".format(
                        self.currentFrame, datetime.datetime.now()
                    )
                )

                print(
                    "Frame has advance current frame is now: {}".format(
                        self.currentFrame
                    )
                )
            if b"Ready" in data and not self.ready:
                self.ready = True
                print("Arduino is now connected on: {}".format(self.serial_port.port))
            elif b"Ready" in data and self.ready:
                print("Arduino has rebooted - terminating")
                exit()

        if write_value != None:
            self.serial_port.write(bytes(write_value, "utf-8"))
            self.commandInProgress = True
        if data != None:
            return data
        else:
            return -1

    def connect(self):
        while not self.ready:
            print("Waiting for Arduino to connect")
            self.serialProcess(None)
            time.sleep(1)



# Very simple class designed to advance frames in another process during
# captures, so a different kernel can handle it and will not delay the
# photograph, or vice versa.

class MotorDriver(Process):

    # Photo capture event.
    capEvent = Event()

    # Application exit event.
    appExitEvent = Event()

    # Queue for receiving orders to the motor control process.
    # The direction of rotation and the number of advance frames will be
    # received.
    motorQueue = Queue()

    # Blocking the connection used for sending images.
    connectionLock = Lock()

    # Shared variable.This variable determines the sending of forward and
    # backward signals of frames, for updating the client's position indicator.
    # 0 -> no update frame
    # 1 -> update frame
    svUpdateFrame = Value("I", 0)

   # Shared variable. Determines the sending of engine stop signals.
    # 0 -> no send stop signals
    # 1 -> send stop signals
    svSendStop = Value("I", 1)

    def __init__(self, capEvent, motExitEvent, motorQueue, connectionLock,
                  svUpdateFrame, svSendStop):
        super(MotorDriver, self).__init__()
        info("Starting MotorDriver")

        self.capEvent = capEvent
        self.motExitEvent = motExitEvent
        self.motorQueue = motorQueue
        self.connectionLock = connectionLock
        self.svUpdateFrame = svUpdateFrame
        self.svSendStop = svSendStop

        # Number of steps required to move forward/backward one frame.
        self.stepsPerFrame = config.stepsPerFrame

        # Variable that contains the order.
        self.order = ""

        # Variable that indicates continuous rotation of the motor if True.
        self.turn = False

        # Variable that determines the number of frames that the motor must
        # advance/reverse.
        self.numframes = 0

    # Main control loop of motor rotation.
    def run(self):
        info("Running motor turn process")

        arduino_control = arduinoInstance(
        serial.Serial(arduino)
    )
        
        arduino.connect()

        try:
            while not self.motExitEvent.is_set():

                if not self.motorQueue.empty():
                    msg = self.motorQueue.get()
                    self.order = msg[0]
                    self.numframes = msg[1]
                else:
                    continue

                if self.order == "f":
                    arduino.write(b'forward')  # Send command to move forward
                    self.turnFrames(self.numframes, "f")
                    # Motor stopped.
                    info("Motor stop")
                    self.capEvent.clear()
                    if self.svSendStop.value:
                        self.sendFrameMove("m")

                elif self.order == "cf":
                    arduino.write(b'forward')  # Send command for continuous forward
                    self.turn = True
                    self.continuousTurn("f")
                    # Motor stopped.
                    info("Motor stop")
                    self.capEvent.clear()
                    if self.svSendStop.value:
                        self.sendFrameMove("m")

                elif self.order == "cb":
                    arduino.write(b'MOTOR_CREV\n')  # Send command for continuous reverse
                    self.turn = True
                    self.continuousTurn("b")
                    sleep(0.5)
                    arduino.write(b'MOTOR_FWD\n')  # Send command to move forward
                    self.turnFrames(1, "f")
                    # Motor stopped.
                    info("Motor stop")
                    self.capEvent.clear()
                    if self.svSendStop.value:
                        self.sendFrameMove("m")

                elif self.order == "b":
                    arduino.write(b'MOTOR_REV\n')  # Send command to move reverse
                    self.turnFrames(self.numframes + 1, "b")
                    sleep(0.5)
                    arduino.write(b'MOTOR_FWD\n')  # Send command to move forward
                    self.turnFrames(1, "f")
                    # Motor stopped.
                    info("Motor stop")
                    self.capEvent.clear()
                    if self.svSendStop.value:
                        self.sendFrameMove("m")

                elif self.order == "s":
                    self.turn = False

        except Exception as e:
            info(getattr(e, 'message', repr(e)))

        finally:
            info("End of the motor turning process")

    def turnFrames(self, numframes, direction):
        if direction == "f":
            if numframes == 1:
                info("1 frame advance")
            else:
                info(str(numframes) + " frames advance")

        if self.order == "b":
            if numframes == 1:
                info("1 frame reverse")
            else:
                info(str(numframes) + " frames reverse")

        for i in range(numframes):
            arduino.write(b'forward')  # Send command to step the motor
            if direction == "f" and self.svUpdateFrame.value:
                self.sendFrameMove("c")
            elif direction == "b" and self.svUpdateFrame.value:
                self.sendFrameMove("C")

            # We check stop order.
            if not self.motorQueue.empty():
                msg = self.motorQueue.get()
                self.order = msg[0]
                if self.order == "s":
                    break

    def continuousTurn(self, direction):
        if direction == "f":
            info("Continuous advance motor")
        elif direction == "b":
            info("Continuous reverse motor")

        # Continuous turning is done by full frames.
        while self.turn:
            arduino.write(b'forward')  # Send command to step the motor
            if direction == "f" and self.svUpdateFrame.value:
                self.sendFrameMove("c")
            elif direction == "b" and self.svUpdateFrame.value:
                self.sendFrameMove("C")

            # We check stop order.
            if not self.motorQueue.empty():
                msg = self.motorQueue.get()
                self.order = msg[0]
                if self.order == "s":
                    self.turn = False
                else:
                    continue

    # Sending forward or backward frame movement signal.
    def sendFrameMove(self, flag):

        with self.connectionLock:

            config.imgConn.write(flag.encode())
            config.imgConn.flush()

        if flag == "c":
            info("Frame advance signal sent")
        elif flag == "C":
            info("Frame reverse signal sent")
        elif flag == "m":
            info("Motor stopped signal sent")
