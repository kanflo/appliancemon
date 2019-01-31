#!/usr/bin/env python3
#
# Not copyrighted at all by Johan Kanflo in 2019 - CC0 applies
#
# Yet another solution for determine if the washing machine, or other appliance,
# has finished by observing the display of the appliance. Oh, it only works if
# the appliance is in a dark room. My washer and dryer is in the basement so
# while this solution is super handy for me it might be useless to you.
#
# Capture a still image, crop and threshold and determine state via the number
# of black pixels in the image. Pixels from the display will be white:
#
#   >= X % black : it's pitch black and the appliance is off
#   >= Y % black : it's pitch black and the appliance is on
#    < Y % black : the light is on, state of appliance is unknown
#
# When one of the two states ("pitch black", "appliance on") toggle, send
# a Pushover.net notification.

import sys
try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("sudo -H pip3 install paho-mqtt")
    sys.exit(1)
try:
    import requests
except ImportError:
    print("sudo -H pip3 install requests")
    sys.exit(1)
import time
import socket
from subprocess import Popen, PIPE
import platform
import re
import logging
from logging.handlers import RotatingFileHandler
import argparse
import configparser
import traceback


# For each iteration, get a stable black level reading. The max difference
# between two measurements has been empirically deducted
max_diff = 3

# Self explanatory
mqtt_debug = False
# Print messages on the console rather than pushing via Pushover.net
pushover_offline = False

"""
Push message via Pushover.net
"""
def pushover_publish(message, title = None, sound = None):
    global config
    if config["PUSHOVER"]["PushoverDisable"] == "yes":
        success = True
    else:
        success = False
        url = "https://api.pushover.net/1/messages.json"
        data = {}
        data["user"] = config["PUSHOVER"]["PushoverUser"]
        data["token"] = config["PUSHOVER"]["PushoverToken"]
        data["device"] = config["PUSHOVER"]["PushoverDevice"]
        data["message"] = message
        if title:
            data["title"] = title
        if sound:
            data["sound"] = sound
        if pushover_offline:
            logging.debug(data)
            success = True
        else:
            resp = requests.post(url, data=data)
            success = resp.status_code == 200
    return success

"""
Simple popen wrapper
"""
def cmd_run(cmd):
    logging.debug(cmd)
    temp = []
    # Duplicated spaces will mess things up...
    for arg in cmd.split(" "):
        if len(arg) > 0:
            temp.append(arg)
    process = Popen(temp, stdout=PIPE, stderr=PIPE)
    stdout, stderr = process.communicate()
    return (stdout, stderr)


"""
Capture an image, store in path, calculate and return the black level, ie. the
percentage of all pixels being black or None if something b0rked.
"""
def get_black_level(path, crop = None, blur = None, threshold = None):
    global config
    if crop == None:
        crop = config["DEFAULT"]["Crop"]
    if blur == None:
        blur = config["DEFAULT"]["Blur"]
    if threshold == None:
        threshold = config["DEFAULT"]["Threshold"]

    if blur == "0x0":
        # Disabled
        blur = ""
    else:
        blur = " -blur " + blur + " "

    if threshold == "0":
        # Disabled
        threshold = ""
    else:
        threshold = " -threshold " + threshold + "% "

    image = "%s/image-%s.jpg" % (path, config["DEFAULT"]["Name"])
    image_proc = "%s/image-proc-%s.png" % (path, config["DEFAULT"]["Name"])
    stdout, stderr = cmd_run("curl -so %s %s" % (image, config["DEFAULT"]["CamURL"]))
    if len(stderr) > 0:
        logging.error("curl failed. %s" % s.decode("utf-8"))
        return None

    stdout, stderr = cmd_run("convert " + image + " -crop " + crop + " " + blur + " " + threshold + " " + image_proc)
    if len(stderr) > 0:
        logging.error("Cropping failed: %s" % stderr.decode("utf-8"))
        return None

    stdout, stderr = cmd_run("convert " + image_proc + " -define histogram:unique-colors=true -format %c histogram:info:-")
    if len(stderr) > 0:
        logging.error("Histogram failed: %s" % stderr.decode("utf-8"))
        return None

    hist = stdout.decode("utf-8")
    pct_black = None
    # Lines are expected to look like "    120000: (  0,  0,  0) #000000 gray(0)"
    # and we want to find '120000' and '#000000'
    num_black = 0
    num_white = 0

    regex = r"\s+(\d+):.+\(.+\).+(#[a-f|A-F|0-9]+)"
    match = re.findall(regex, hist)
    if match:
        for m in match:
            if m[1].lower() == "#ffffff":
                num_white = int(m[0])
            elif m[1] == "#000000":
                num_black = int(m[0])
    else:
        logging.error("Black level regexp error")
        return None

    if num_black == 0 and num_white == 0:
        logging.debug("Neither any black nor any white pixels")
        return 0

    pct_black = 100.0 * (num_black / (num_black + num_white))

    logging.debug("%d%% black" % pct_black)
    return pct_black


"""
MQTT callbacks
"""
def on_connect(client, userdata, flags, rc):
    if rc==0:
        client.connected_flag=True #set flag
        logging.debug("MQTT connected OK")
    else:
        logging.error("MQTT bad connection, code=", rc)
        client.bad_connection_flag=True


def on_disconnect(client, userdata, rc):
    logging.info("disconnecting reason  "  +str(rc))
    client.connected_flag=False
    client.disconnect_flag=True


def on_message(client, userdata, message):
    logging.debug("message received " ,str(message.payload.decode("utf-8")))
    logging.debug("message topic=",message.topic)
    logging.debug("message qos=",message.qos)
    logging.debug("message retain flag=",message.retain)


def on_log(client, userdata, level, buf):
    if mqtt_debug:
        logging.debug("log: ",buf)


def on_publish(client, userdata, mid):
    if mqtt_debug:
        logging.debug("published: ", userdata, mid)


def main():
    try:
        global config
        parser = argparse.ArgumentParser(description="This script monitors your washing machine or other appliance")
        parser.add_argument("-t", "--test", action='append', help="Test image processing parameters",)
        parser.add_argument("-v", "--verbose", help="Increase output verbosity", action="store_true")
        parser.add_argument("-c", "--config", action="store", help="Configuration file", default="sampleconfig.yml")
        args = parser.parse_args()

        config = configparser.ConfigParser()
        try:
            config.read(args.config, encoding='utf-8')
        except Exception as e:
            print("Failed to read config file: %s" % str(e))
            print("use --create-config <filename> to create a default configuration file")
            sys.exit(1)

        if args.test:
            options = args.test[0].split(" ")
            if len(options) != 3:
                print("Error: %s -t \"<crop> <blur> <threshold>\"" % sys.argv[0])
                print("  <crop>      : {X}x{Y}+{width}+{height}")
                print("  <blur>      : {radius}x{sigma}")
                print("  <threshold> : {t}")
                print("    Eg. %s -t \"300x150+30+365 0x6 6\"" % sys.argv[0])
                sys.exit(1)
            pct = get_black_level(".", options[0], options[1], options[2])
            print("Black level: %d%%" % pct)
            sys.exit(0)

        level = logging.DEBUG if args.verbose else logging.WARNING

        log_formatter = logging.Formatter('%(asctime)s %(levelname)s %(funcName)s(%(lineno)d) %(message)s')
        logFile = config["DEFAULT"]["TempDir"] + ("/appliancemon-%s.log" % config["DEFAULT"]["Name"])
        try:
            my_handler = RotatingFileHandler(logFile, mode='a', maxBytes=100*1024, backupCount=1, encoding=None, delay=0)
        except FileNotFoundError:
            print("Failed to create %s" % logFile)
            sys.exit(1)
        my_handler.setFormatter(log_formatter)
        app_log = logging.getLogger()
        app_log.addHandler(my_handler)
        app_log.setLevel(level)

        logging.debug('---------------------------------------------------------')
        logging.debug('App started')

        mqtt.Client.bad_connection_flag = False
        mqtt.Client.connected_flag = False

        broker = config["MQTT"]["MQTTBroker"]
        client = mqtt.Client("appliancemon-%s" % config["DEFAULT"]["Name"])
        client.on_connect = on_connect
        client.on_message = on_message
        client.on_log = on_log
        client.on_publish = on_publish

        logging.info("Connecting to broker %s" % broker)
        while True:
            try:
                client.connect(broker)
                break
            except socket.gaierror:
                logging.debug("Warning: could not connect to %s, retry in 10s" % broker)
                time.sleep(10)

        if args.verbose:
            pushover_publish(title = config["PUSHOVER"]["PushoverTitle"], message = "Appliance Monitor online")

        if config["MQTT"]["MQTTDisable"] == 'no':
            client.publish(config["MQTT"]["MQTTApplianceTopic"], payload="online")
        logging.debug("Connected to MQTT broker")


        # Current state unknown at start
        old_room_dark = None
        old_appliance_on = None

        while not client.connected_flag and not client.bad_connection_flag:
            diff = max_diff + 1
            b2 = get_black_level(config["DEFAULT"]["TempDir"])
            num_checks = 5
            while diff > max_diff and num_checks > 0:
                b1 = b2
                time.sleep(int(config["DEFAULT"]["StablePeriodSleep"]))
                b2 = get_black_level(config["DEFAULT"]["TempDir"])
                if b1 and b2:
                    diff =  abs(b1 - b2)
                num_checks -= 1
            b = (b1 + b2) / 2
            # We assign the old states to the new ones to begin with as we may not
            # find any new states below meaning we could get a NameError exception.
            new_appliance_on = old_appliance_on
            new_room_dark = old_room_dark

            if b > int(config["DEFAULT"]["PitchDarkLevel"]):
                new_room_dark = True
                new_appliance_on = False
            elif b > int(config["DEFAULT"]["MachineOnLevel"]):
                new_room_dark = True
                new_appliance_on = True
            else:
                new_room_dark = False

            if old_appliance_on != None:
                if old_appliance_on != new_appliance_on:
                    if not new_appliance_on:
                        pushover_publish(title = config["PUSHOVER"]["PushoverTitle"], message = config["PUSHOVER"]["PushoverMessageApplicanceDone"])
                    if config["MQTT"]["MQTTDisable"] == 'no':
                        status = client.publish(config["MQTT"]["MQTTApplianceTopic"] + "/state", payload=("on" if new_appliance_on else "off"))

            if old_room_dark != None:
                if old_room_dark != new_room_dark:
                    if config["DEFAULT"]["ReportLightChange"] == "yes":
                        pushover_publish(title = config["PUSHOVER"]["PushoverTitle"], message = config["PUSHOVER"]["PushoverMessageLightsOff"] if new_room_dark else config["PUSHOVER"]["PushoverMessageLightsOn"])
                        if config["MQTT"]["MQTTDisable"] == 'no':
                            status = client.publish(config["MQTT"]["MQTTLightsTopic"] + "/lights", payload=("off" if new_room_dark else "on"))

            old_room_dark = new_room_dark
            old_appliance_on = new_appliance_on

            if config["DEFAULT"]["ReportBlackLevel"] == "yes":
                status = client.publish(config["MQTT"]["MQTTApplianceTopic"] + "/blacklevel", payload=int(round(b)))

            time.sleep(int(config["DEFAULT"]["LoopPeriodSleep"]))

        if client.bad_connection_flag:
            client.loop_stop()
            sys.exit()

        client.loop_stop()
        client.disconnect()
    except Exception as e:
        logging.error("Exception occurred", exc_info=True)
        tb = traceback.format_exc()
        print("Exception %s\n%s" % (str(e), tb))
        pushover_publish(title = config["PUSHOVER"]["PushoverTitle"], message = "Appliance Monitor crashed: %s %s" % (str(e), tb))


if __name__ == "__main__":
    main()
