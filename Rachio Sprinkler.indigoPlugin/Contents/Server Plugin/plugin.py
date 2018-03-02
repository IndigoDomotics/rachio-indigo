#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################
# Copyright (c) 2014, Perceptive Automation, LLC. All rights reserved.
# http://www.indigodomo.com

try:
    import indigo
except:
    pass

import requests
import json
import copy
import traceback
from operator import itemgetter
from datetime import datetime, timedelta, time
from dateutil import tz
from distutils.version import LooseVersion

RACHIO_API_VERSION              = "1"
RACHIO_MAX_ZONE_DURATION        = 10800
DEFAULT_API_CALL_TIMEOUT        = 5      # number of seconds after which we timeout any network calls
MINIMUM_POLLING_INTERVAL        = 3      # number of minutes between each poll, default is 3 (changed 2/27/2018 to help avoid throttling)
DEFAULT_WEATHER_UPDATE_INTERVAL = 10     # number of minutes between each forecast update, default is 10
THROTTLE_LIMIT_TIMER            = 61     # number of minutes to wait if we've received a throttle error before doing any API calls

API_URL                         = "https://api.rach.io/{apiVersion}/public/"
PERSON_URL                      = API_URL + "person/{personId}"
PERSON_INFO_URL                 = PERSON_URL.format(apiVersion=RACHIO_API_VERSION, personId="info")
DEVICE_BASE_URL                 = API_URL + "device/"
DEVICE_GET_URL                  = DEVICE_BASE_URL + "{deviceId}"
DEVICE_CURRENT_SCHEDULE_URL     = DEVICE_GET_URL + "/current_schedule"
DEVICE_STOP_WATERING_URL        = DEVICE_BASE_URL + "stop_water"
DEVICE_TURN_OFF_URL             = DEVICE_BASE_URL + "off"
DEVICE_TURN_ON_URL              = DEVICE_BASE_URL + "on"
DEVICE_GET_FORECAST_URL         = DEVICE_GET_URL + "/forecast?units={units}"
ZONE_URL                        = API_URL + "zone/"
ZONE_START_URL                  = ZONE_URL + "start"
SCHEDULERULE_URL                = API_URL + "schedulerule/{scheduleRuleId}"
SCHEDULERULE_START_URL          = SCHEDULERULE_URL.format(apiVersion=RACHIO_API_VERSION, scheduleRuleId="start")
SCHEDULERULE_SEASONAL_ADJ_URL   = SCHEDULERULE_URL.format(apiVersion=RACHIO_API_VERSION, scheduleRuleId="seasonal_adjustment")

FORECAST_FIELDS_USED = {
    "calculatedPrecip": "decimalPlaces:2",
    "cloudCover": "percentage",
    "currentTemperature": "decimalPlaces:1",
    "dewPoint": "decimalPlaces:1",
    "humidity": "percentage",
    "iconUrl": "",
    "precipIntensity": "decimalPlaces:4",
    "precipProbability": "percentage",
    "temperatureMax": "decimalPlaces:1",
    "temperatureMin": "decimalPlaces:1",
    "weatherSummary": "",
    "weatherType": "",
    "windSpeed": "decimalPlaces:2",
}

ALL_OPERATIONAL_ERROR_EVENTS = {
    "startZoneFailed",
    "stopFailed",
    "startRachioScheduleFailed",
    "setSeasonalAdjustmentFailed",
    "setStandbyFailed",
}

ALL_COMM_ERROR_EVENTS = {
    "personCall",
    "personInfoCall",
    "getScheduleCall",
    "forecastCall",
}

class ThrottleDelayError(Exception):
    pass

def convert_timestamp(timestamp):
    from_zone = tz.tzutc()
    to_zone = tz.tzlocal()
    time_utc = datetime.utcfromtimestamp(timestamp/1000)
    time_utc_gmt = time_utc.replace(tzinfo=from_zone)
    return time_utc_gmt.astimezone(to_zone)


################################################################################
class Plugin(indigo.PluginBase):
    ########################################
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super(Plugin, self).__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        # Used to control when to show connection errors (vs just repeated retries)
        self._displayed_connection_error = False
        self.debug = pluginPrefs.get("showDebugInfo", False)
        # Not currently exposed in the plugin prefs, but we can if necessary
        self.pollingInterval = int(pluginPrefs.get("pollingInterval", MINIMUM_POLLING_INTERVAL))
        # Not currently exposed in the plugin prefs, but we can if necessary
        self.timeout = int(pluginPrefs.get("apiTimeout", DEFAULT_API_CALL_TIMEOUT))

        self.access_token = pluginPrefs.get("accessToken", None)
        self.person_id = pluginPrefs.get("personId", None)
        if self.access_token:
            self.headers = {
                "Authorization": "Bearer {}".format(self.access_token)
            }
        else:
            self.logger.warn("You must specify your API token in the plugin's config before the plugin can be used.")
            self.headers = None
        self.triggerDict = {}
        self._next_weather_update = datetime.now()
        self.throttle_next_call = None
        self.version_check_email = pluginPrefs.get("versionCheckEmail", None)
        # Check for updates at 8pm local time every day. Not that it matters that much, but seems like it's about the
        # best time in general.
        now = datetime.now()
        target_date = now.date()
        target_time = time(20)
        if target_time < now.time():
            target_date = (now + timedelta(days=1)).date()
        self.next_version_check = datetime(target_date.year, target_date.month, target_date.day, 20)

    ########################################
    # Internal helper methods
    ########################################
    def _version_check(self, pluginId=None):
        if not pluginId:
            pluginId = self.pluginId
        # Create some URLs we'll use later on
        current_version_url = "https://api.indigodomo.com/api/v2/pluginstore/plugin-version-info.json?pluginId={}".format(
            pluginId
        )
        store_detail_url = "https://www.indigodomo.com/pluginstore/{}/"
        try:
            # GET the url from the servers with a short timeout (avoids hanging the plugin)
            reply = requests.get(current_version_url, timeout=5)
            # This will raise an exception if the server returned an error
            reply.raise_for_status()
            # We now have a good reply so we get the json
            reply_dict = reply.json()
            plugin_dict = reply_dict["plugins"][0]
            # Make sure that the 'latestRelease' element is a dict (could be a string for built-in plugins).
            latest_release = plugin_dict["latestRelease"]
            if isinstance(latest_release, dict):
                # Compare the current version with the one returned in the reply dict
                if LooseVersion(latest_release["number"]) > LooseVersion(self.pluginVersion):
                    # The release in the store is newer than the current version.
                    # We'll do a couple of things: first, we'll just log it
                    self.logger.info(
                        "A new version of the plugin (v{}) is available at: {}".format(
                            latest_release["number"],
                            store_detail_url.format(plugin_dict["id"])
                        )
                    )
                    # We'll change the value of a variable named "Plugin_Name_Current_Version" to the new version number
                    # which the user can then build a trigger on (or whatever).
                    try:
                        variable_name = u"{}_Available_Version".format(self.pluginDisplayName.replace(" ", "_"))
                        indigo.variable.updateValue(variable_name, latest_release["number"])
                    except:
                        # The variable probably doesn't exist - we don't care so just skip it
                        pass
                    # Fire the version update event so users can trigger from it and do whatever they want.
                    self._fireTrigger("updateAvailable")
                else:
                    self.logger.info(u"No plugin updates available.")
        except Exception as exc:
            self.logger.error(unicode(exc))
        self.next_version_check = datetime.now() + timedelta(days=1)

    ########################################
    def _make_api_call(self, url, request_method="get", data=None):
        try:
            if self.throttle_next_call:
                if self.throttle_next_call > datetime.now():
                    # Too soon, raise exception
                    raise ThrottleDelayError("API calls have violated rate limit - next connection attempt at {:%H:%M:%S}".format(
                        self.throttle_next_call))
                else:
                    self.throttle_next_call = None
            return_val = None
            if request_method == "put":
                method = requests.put
            elif request_method == "post":
                method = requests.post
            elif request_method == "delete":
                method = requests.delete
            else:
                method = requests.get
            if data and request_method in ["put", "post"]:
                r = method(url, data=json.dumps(data), headers=self.headers, timeout=self.timeout)
            else:
                r = method(url, headers=self.headers, timeout=self.timeout)
            if r.status_code == 200:
                return_val = r.json()
            elif r.status_code == 204:
                return_val = True
            else:
                r.raise_for_status()
            self._displayed_connection_error = False
            return return_val
        except requests.exceptions.ConnectionError as exc:
            if not self._displayed_connection_error:
                self.logger.error("Connection to Rachio API server failed. Will continue to retry silently.")
                self._displayed_connection_error = True
            raise exc
        except requests.exceptions.ReadTimeout as exc:
            if not self._displayed_connection_error:
                self.logger.error("Unable to contact device - the controller may be offline. Will continue to retry silently.")
                self._displayed_connection_error = True
            raise exc
        except requests.exceptions.Timeout as exc:
            if not self._displayed_connection_error:
                self.logger.error("Connection to Rachio API server failed. Will continue to retry silently.")
                self._displayed_connection_error = True
            raise exc
        except requests.exceptions.HTTPError as exc:
            if exc.response.status == 429:
                # We've hit the throttle limit - we need to back off on all requests for some period of time
                self.throttle_next_call = datetime.now()+timedelta(minutes=THROTTLE_LIMIT_TIMER)
                self._fireTrigger("rateLimitExceeded")
            raise exc
        except ThrottleDelayError as exc:
            self.logger.error(str(exc))
            self.logger.debug("{}:\n{}".format(str(exc), traceback.format_exc(10)))
            raise exc
        except Exception as exc:
            self.logger.error("Connection to Rachio API server failed with exception: {}. Check the log file for full details.".format(exc.__class__.__name__))
            self.logger.debug("Connection to Rachio API server failed with exception:\n{}".format(traceback.format_exc(10)))
            raise exc

    ########################################
    def _get_device_dict(self, id):
        dev_list = [dev_dict for dev_dict in self.person["devices"] if dev_dict["id"] == id]
        if len(dev_list):
            return dev_list[0]
        else:
            return None

    ########################################
    def _get_zone_dict(self, id, zoneNumber):
        dev_dict = self._get_device_dict(id)
        if dev_dict:
            zone_list = [zone_dict for zone_dict in dev_dict["zones"] if zone_dict["zoneNumber"] == zoneNumber]
            if len(zone_list):
                return zone_list[0]
        return None

    ########################################
    def _update_from_rachio(self):
        try:
            if self.access_token:
                if not self.person_id:
                    try:
                        reply = self._make_api_call(PERSON_INFO_URL)
                        self.person_id = reply["id"]
                        self.pluginPrefs["personId"] = self.person_id
                    except Exception as exc:
                        self.logger.error("Error getting user ID from Rachio via API.")
                        self.logger.debug("API error: \n{}".format(traceback.format_exc(10)))
                        self._fireTrigger("personCall")
                        return
                try:
                    reply_dict = self._make_api_call(PERSON_URL.format(apiVersion=RACHIO_API_VERSION, personId=self.person_id))
                    self.person = reply_dict
                    self.rachio_devices = self.person["devices"]
                except Exception as exc:
                    self.logger.error("Error getting user data from Rachio via API.")
                    self.logger.debug("API error: \n{}".format(traceback.format_exc(10)))
                    self._fireTrigger("personInfoCall")
                    return
                current_device_uuids = [s.states["id"] for s in indigo.devices.iter(filter="self.sprinkler")]
                self.unused_devices = {dev_dict["id"]: dev_dict for dev_dict in self.person["devices"] if dev_dict["id"] not in current_device_uuids}
                self.defined_devices = {dev_dict["id"]: dev_dict for dev_dict in self.person["devices"] if dev_dict["id"] in current_device_uuids}
                defined_devices = [dev_dict for dev_dict in self.person["devices"] if dev_dict["id"] in current_device_uuids]
                for dev in [s for s in indigo.devices.iter(filter="self.sprinkler") if s.enabled]:
                    for dev_dict in defined_devices:
                        # Find the matching update dict for the device
                        if dev_dict["id"] == dev.states["id"]:
                            # Update any changed information for the device - we only look at the data that may change
                            # as part of operation - anything that's fixed (serial number, etc. gets set once when the
                            # device is created or when the user replaces the controller.
                            update_list = []
                            # "status" is ONLINE or OFFLINE - if the latter it's unplugged or otherwise can't communicate with the cloud
                            # note: it often takes a REALLY long time for the API to return OFFLINE and sometimes it never does.
                            if dev_dict["status"] != dev.states["status"]:
                                update_list.append({"key": "status", 'value': dev_dict["status"]})
                                if dev_dict["status"] == "OFFLINE":
                                    dev.setErrorStateOnServer('unavailable')
                                else:
                                    dev.setErrorStateOnServer('')
                            # "on" is False if the controller is in Standby Mode - note: it will still react to commands
                            if not dev_dict["on"] != dev.states["inStandbyMode"]:
                                update_list.append({"key": "inStandbyMode", 'value': not dev_dict["on"]})
                            if dev_dict["name"] != dev.states["name"]:
                                update_list.append({"key": "name", "value": dev_dict["name"]})
                            if dev_dict["scheduleModeType"] != dev.states["scheduleModeType"]:
                                update_list.append({"key": "scheduleModeType", "value": dev_dict["scheduleModeType"]})
                            # Update location-based stuff
                            if dev_dict["elevation"] != dev.states["elevation"]:
                                update_list.append({"key": "elevation", "value": dev_dict["elevation"]})
                            if dev_dict["latitude"] != dev.states["latitude"]:
                                update_list.append({"key": "latitude", "value": dev_dict["latitude"]})
                            if dev_dict["longitude"] != dev.states["longitude"]:
                                update_list.append({"key": "latitude", "value": dev_dict["latitude"]})
                            if dev_dict["timeZone"] != dev.states["timeZone"]:
                                update_list.append({"key": "timeZone", "value": dev_dict["timeZone"]})
                            if dev_dict["utcOffset"] != dev.states["utcOffset"]:
                                update_list.append({"key": "utcOffset", "value": dev_dict["utcOffset"]})

                            activeScheduleName = None
                            # Get the current schedule for the device - it will tell us if it's running or not
                            try:
                                current_schedule_dict = self._make_api_call(DEVICE_CURRENT_SCHEDULE_URL.format(apiVersion=RACHIO_API_VERSION, deviceId=dev.states["id"]))
                                if len(current_schedule_dict):
                                    # Something is running, so we need to figure out if it's a manual or automatic schedule and
                                    # if it's automatic (a Rachio schedule) then we need to get the name of that schedule
                                    update_list.append({"key": "activeZone", "value": current_schedule_dict["zoneNumber"]})
                                    if current_schedule_dict["type"] == "AUTOMATIC":
                                        schedule_detail_dict = self._make_api_call(SCHEDULERULE_URL.format(apiVersion=RACHIO_API_VERSION, scheduleRuleId=current_schedule_dict["scheduleRuleId"]))
                                        update_list.append({"key": "activeSchedule", "value": schedule_detail_dict["name"]})
                                        activeScheduleName = schedule_detail_dict["name"]

                                    else:
                                        update_list.append({"key": "activeSchedule", "value": current_schedule_dict["type"].title()})
                                        activeScheduleName = current_schedule_dict["type"].title()
                                else:
                                    update_list.append({"key": "activeSchedule", "value": "No active schedule"})
                                    # Show no zones active
                                    update_list.append({"key": "activeZone", "value": 0})
                            except Exception as exc:
                                update_list.append({"key": "activeSchedule", "value": "Error getting current schedule"})
                                self.logger.debug("API error: \n{}".format(traceback.format_exc(10)))
                                self._fireTrigger("getScheduleCall")
                            # Send the state updates to the server
                            if len(update_list):
                                dev.updateStatesOnServer(update_list)
                            # Update zone information as necessary - these are properties, not states.
                            zoneNames = ""
                            maxZoneDurations = ""
                            for zone in sorted(dev_dict["zones"], key=itemgetter('zoneNumber')):
                                zoneNames += ", {}".format(zone["name"]) if len(zoneNames) else zone["name"]
                                if len(maxZoneDurations):
                                    maxZoneDurations += ", {}".format(zone["maxRuntime"]) if zone["enabled"] else ", 0"
                                else:
                                    maxZoneDurations = "{}".format(zone["maxRuntime"]) if zone["enabled"] else "0"
                            props = copy.deepcopy(dev.pluginProps)
                            props["NumZones"] = len(dev_dict["zones"])
                            props["ZoneNames"] = zoneNames
                            props["MaxZoneDurations"] = maxZoneDurations
                            if activeScheduleName:
                                props["ScheduledZoneDurations"] = activeScheduleName
                            dev.replacePluginPropsOnServer(props)
                    self._update_forecast_data(dev)
            else:
                self.logger.warn("You must specify your API token in the plugin's config before the plugin can be used.")
        except Exception as exc:
            self.logger.error("Unknown error:\n{}".format(traceback.format_exc(10)))

    ########################################
    def _update_forecast_data(self, dev):
        if datetime.now() >= self._next_weather_update:
            try:
                units = dev.pluginProps["units"]
                reply_dict = self._make_api_call(
                    DEVICE_GET_FORECAST_URL.format(
                        apiVersion=RACHIO_API_VERSION,
                        deviceId=dev.states["id"],
                        units=units
                    )
                )
                current_conditions = reply_dict["current"]
                state_update_list = []
                for k, v in current_conditions.iteritems():
                    if k in FORECAST_FIELDS_USED:
                        update_dict = {"key": "current_{}".format(k), "value": v}
                        if "decimalPlaces" in FORECAST_FIELDS_USED[k]:
                            update_dict["decimalPlaces"]= FORECAST_FIELDS_USED[k].split(":")[1]
                        elif "percentage" in FORECAST_FIELDS_USED[k]:
                            update_dict["value"] = v * 100
                            update_dict["decimalPlaces"] = 0
                            update_dict["uiValue"] = u"{}%".format(update_dict["value"])
                        if u"currentTemperature" in k or u"dewPoint" in k:
                            update_dict["uiValue"] = u"{} °{}".format(update_dict["value"], u"F" if units == "US" else u"C")
                        state_update_list.append(update_dict)
                        if k == u"currentTemperature":
                            pass
                forecasts_sorted = sorted(reply_dict["forecast"], key=itemgetter("time"))
                for count, forecast in enumerate(forecasts_sorted):
                    for k, v in forecast.iteritems():
                        # For some strange reason, the API started returning currentTemperature for forecast days, which
                        # makes no sense because it's a FORECAST (nothing current about it). So, we'll just ignore it.
                        if k in FORECAST_FIELDS_USED and k != "currentTemperature":
                            update_dict = {"key": "t{}forecast_{}".format(count, k), "value": v}
                            if "decimalPlaces" in FORECAST_FIELDS_USED[k]:
                                update_dict["decimalPlaces"]= FORECAST_FIELDS_USED[k].split(":")[1]
                            elif "percentage" in FORECAST_FIELDS_USED[k]:
                                update_dict["value"] = v * 100
                                update_dict["decimalPlaces"] = 0
                                update_dict["uiValue"] = u"{}%".format(update_dict["value"])
                            if "temperature" in k or "dewPoint" in k:
                                update_dict["uiValue"] = u"{} °{}".format(update_dict["value"], u"F" if units == "US" else u"C")
                            state_update_list.append(update_dict)
                dev.updateStatesOnServer(state_update_list)
                self._next_weather_update = datetime.now() + timedelta(seconds=DEFAULT_WEATHER_UPDATE_INTERVAL)
            except Exception as exc:
                self.logger.error("Error getting forecast data from Rachio via API.")
                self.logger.debug("API error: \n{}".format(traceback.format_exc(10)))
                self._fireTrigger("forecastCall")

    ########################################
    # startup, concurrent thread, and shutdown methods
    ########################################
    def startup(self):
        pass

    ########################################
    def shutdown(self):
        pass

    ########################################
    def runConcurrentThread(self):
        self.logger.debug("Starting concurrent tread")
        try:
            # Polling - if we ever implement a webhook catcher from the Rachio API we would no longer need to poll.
            # But since that would currently require an IWS handler and some way to communicate between the two we'll
            # save that for later when the two APIs are integrated.
            while True:
                if self.next_version_check < datetime.now():
                    self._version_check()
                try:
                    self._update_from_rachio()
                except:
                    pass
                self.sleep(self.pollingInterval*60)
        except self.StopThread:
            self.logger.debug("Received StopThread")

    ########################################
    # Dialog list callbacks
    ########################################
    def availableControllers(self, filter="", valuesDict=None, typeId="", targetId=0):
        controller_list = [(id, dev_dict['name']) for id, dev_dict in self.unused_devices.iteritems()]
        dev = indigo.devices.get(targetId, None)
        if dev and dev.configured:
            dev_dict = self._get_device_dict(dev.states["id"])
            controller_list.append((dev.states["id"], dev_dict["name"]))
        return controller_list

    ########################################
    def availableSchedules(self, filter="", valuesDict=None, typeId="", targetId=0):
        schedule_list = []
        dev = indigo.devices.get(targetId, None)
        if dev:
            dev_dict = self._get_device_dict(dev.states["id"])
            schedule_list = [(rule_dict["id"], rule_dict['name']) for rule_dict in dev_dict["scheduleRules"]]
        return schedule_list

    ########################################
    def sprinklerList(self, filter="", valuesDict=None, typeId="", targetId=0):
        return [(s.id, s.name) for s in indigo.devices.iter(filter="self.sprinkler")]

    ########################################
    # Validation callbacks
    ########################################
    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        if devId:
            dev = indigo.devices[devId]
            if dev.pluginProps.get("id", None) != valuesDict["id"]:
                valuesDict["configured"] = False
        else:
            valuesDict["configured"] = False
        return (True, valuesDict)

    ########################################
    def validateActionConfigUi(self, valuesDict, typeId, devId):
        errorsDict = indigo.Dict()
        if typeId == "setSeasonalAdjustment":
            try:
                if int(valuesDict["adjustment"]) not in range(-100, 100):
                    raise Exception()
            except:
                errorsDict["adjustment"] = "Must be an integer from -100 to 100 (a percentage)"
        if len(errorsDict):
            return False, valuesDict, errorsDict
        return True, valuesDict

    ########################################
    def validateEventConfigUi(self, valuesDict, typeId, devId):
        errorsDict = indigo.Dict()
        if typeId == "sprinklerError":
            if valuesDict["id"] == "":
                errorsDict["id"] = u"You must select a Rachio Sprinkler device."
        if len(errorsDict):
            return False, valuesDict, errorsDict
        return True, valuesDict

    ########################################
    def validatePrefsConfigUi(self, valuesDict):
        errorsDict = indigo.Dict()
        try:
            if int(valuesDict['pollingInterval']) < 3:
                raise Exception()
        except:
            errorsDict["pollingInterval"] = u"Must be a number greater than or equal to 3 (minutes)."
        if len(errorsDict):
            return False, valuesDict, errorsDict
        return True, valuesDict

    ########################################
    # General device callbacks
    ########################################
    def didDeviceCommPropertyChange(self, origDev, newDev):
        return True if origDev.states["id"] != newDev.states["id"] else False

    ########################################
    def deviceStartComm(self, dev):
        if not dev.pluginProps["configured"]:
            # Get the full device info and update the newly created device
            dev_dict = self.unused_devices.get(dev.pluginProps["id"], None)
            if dev_dict:
                # Update all the states here
                update_list = []
                update_list.append({"key": "id", "value": dev_dict["id"]})
                update_list.append({"key": "address", "value": dev_dict["macAddress"]})
                update_list.append({"key": "model", "value": dev_dict["model"]})
                update_list.append({"key": "serialNumber", "value": dev_dict["serialNumber"]})
                update_list.append({"key": "elevation", "value": dev_dict["elevation"]})
                update_list.append({"key": "latitude", "value": dev_dict["latitude"]})
                update_list.append({"key": "longitude", "value": dev_dict["longitude"]})
                update_list.append({"key": "name", "value": dev_dict["name"]})
                update_list.append({"key": "inStandbyMode", "value": not dev_dict["on"]})
                update_list.append({"key": "paused", "value": dev_dict["paused"]})
                update_list.append({"key": "scheduleModeType", "value": dev_dict["scheduleModeType"]})
                update_list.append({"key": "status", "value": dev_dict["status"]})
                update_list.append({"key": "timeZone", "value": dev_dict["timeZone"]})
                update_list.append({"key": "utcOffset", "value": dev_dict["utcOffset"]})
                # Get the current schedule for the device - it will tell us if it's running or not
                activeScheduleName = None
                try:
                    current_schedule_dict = self._make_api_call(DEVICE_CURRENT_SCHEDULE_URL.format(apiVersion=RACHIO_API_VERSION, deviceId=dev_dict["id"]))
                    if len(current_schedule_dict):
                        # Something is running, so we need to figure out if it's a manual or automatic schedule and
                        # if it's automatic (a Rachio schedule) then we need to get the name of that schedule
                        update_list.append({"key": "activeZone", "value": current_schedule_dict["zoneNumber"]})
                        if current_schedule_dict["type"] == "AUTOMATIC":
                            schedule_detail_dict = self._make_api_call(SCHEDULERULE_URL.format(apiVersion=RACHIO_API_VERSION, scheduleRuleId=current_schedule_dict["scheduleRuleId"]))
                            update_list.append({"key": "activeSchedule", "value": schedule_detail_dict["name"]})
                            activeScheduleName = schedule_detail_dict["name"]
                        else:
                            update_list.append(
                                {"key": "activeSchedule", "value": current_schedule_dict["type"].title()})
                            activeScheduleName = current_schedule_dict["type"].title()
                    else:
                        update_list.append({"key": "activeSchedule", "value": "No active schedule"})
                        # Show no zones active
                        update_list.append({"key": "activeZone", "value": 0})
                except:
                    update_list.append({"key": "activeSchedule", "value": "Error getting current schedule"})
                    self.logger.debug("API error: \n{}".format(traceback.format_exc(10)))
                # Send the state updates to the server
                if len(update_list):
                    dev.updateStatesOnServer(update_list)

                # Update zone information as necessary - these are properties, not states.
                zoneNames = ""
                maxZoneDurations = ""
                for zone in sorted(dev_dict["zones"], key=itemgetter('zoneNumber')):
                    zoneNames += ", {}".format(zone["name"]) if len(zoneNames) else zone["name"]
                    if len(maxZoneDurations):
                        maxZoneDurations += ", {}".format(zone["maxRuntime"]) if zone["enabled"] else ", 0"
                    else:
                        maxZoneDurations = "{}".format(zone["maxRuntime"]) if zone["enabled"] else "0"
                props = copy.deepcopy(dev.pluginProps)
                props["NumZones"] = len(dev_dict["zones"])
                props["ZoneNames"] = zoneNames
                props["MaxZoneDurations"] = maxZoneDurations
                if activeScheduleName:
                    props["ScheduledZoneDurations"] = activeScheduleName
                props["configured"] = True
                props["apiVersion"] = RACHIO_API_VERSION
                dev.replacePluginPropsOnServer(props)
                self._next_weather_update = datetime.now()
                self._update_forecast_data(dev)
            else:
                self.logger.error("Rachio device '{}' configured with unknown ID. Reconfigure the device to make it active.".format(dev.name))

    ########################################
    def deviceStopComm(self, dev):
        pass

    ########################################
    # Event callbacks
    ########################################
    #  All things that could trigger an event call this method which will do the dispatch
    ########################################
    def _fireTrigger(self, event, dev_id=None):
        try:
            for triggerId, trigger in self.triggerDict.items():
                if trigger.pluginTypeId == "sprinklerError":
                    if int(trigger.pluginProps["id"]) == dev_id:
                        # for the all trigger type, we fire any event that's in the ALL_OPERATIONAL_ERROR_EVENTS
                        # list we defined at the top.
                        type = trigger.pluginProps["errorType"]
                        if type == "all" and event in ALL_OPERATIONAL_ERROR_EVENTS:
                            indigo.trigger.execute(trigger)
                        # then we fire if the event specifically matches the trigger type
                        if type == event:
                            indigo.trigger.execute(trigger)
                elif trigger.pluginTypeId == "commError":
                    type = trigger.pluginProps["errorType"]
                    # first we fire the trigger if it's any comm error in the ALL_COMM_ERROR_EVENTS list
                    if type == "allCommErrors" and event in ALL_COMM_ERROR_EVENTS:
                        indigo.trigger.execute(trigger)
                    # then we fire if the event specifically matches the trigger type
                    if type == event:
                        indigo.trigger.execute(trigger)
                elif trigger.pluginTypeId == event:
                    # an update is available, just fire the trigger since there's nothing else to look at
                    indigo.trigger.execute(trigger)
        except Exception as exc:
            self.logger.error(u"An error occurred during trigger processing")
            self.logger.debug(u"An error occurred during trigger processing: \n{}".format(traceback.format_exc(10)))

    ########################################
    def triggerStartProcessing(self, trigger):
        super(Plugin, self).triggerStartProcessing(trigger)
        self.logger.debug("Start processing trigger " + str(trigger.id))
        if trigger.id not in self.triggerDict:
            self.triggerDict[trigger.id] = trigger
        self.logger.debug("Start trigger processing list: " + str(self.triggerDict))

    ########################################
    def triggerStopProcessing(self, trigger):
        super(Plugin, self).triggerStopProcessing(trigger)
        self.logger.debug("Stop processing trigger " + str(trigger.id))
        try:
            del self.triggerDict[trigger.id]
        except:
            # the trigger isn't in the list for some reason so just skip it
            pass
        self.logger.debug("Stop trigger processing list: " + str(self.triggerDict))

    ########################################
    # Sprinkler Control Action callback
    ########################################
    def actionControlSprinkler(self, action, dev):
        ###### ZONE ON ######
        if action.sprinklerAction == indigo.kSprinklerAction.ZoneOn:
            if self.throttle_next_call:
                self.logger.error("API calls have violated rate limit - next connection attempt at {:%H:%M:%S}".format(
                    self.throttle_next_call)
                )
                self._fireTrigger("startZoneFailed", dev.id)
            else:
                zone_dict = self._get_zone_dict(dev.states["id"], action.zoneIndex)
                if zone_dict:
                    zoneName = zone_dict["name"]
                    data = {
                        "id": zone_dict["id"],
                        "duration": zone_dict["maxRuntime"] if zone_dict["maxRuntime"] <= RACHIO_MAX_ZONE_DURATION else RACHIO_MAX_ZONE_DURATION,
                    }
                    try:
                        self._make_api_call(ZONE_START_URL.format(apiVersion=RACHIO_API_VERSION), request_method="put", data=data)
                        self.logger.info(u'sent "{} - {}" on'.format(dev.name, zoneName))
                        dev.updateStateOnServer("activeZone", action.zoneIndex)
                    except:
                        # Else log failure but do NOT update state on Indigo Server. Also, fire any triggers the user has
                        # on zone start failures.
                        self.logger.error(u'send "{} - {}" on failed'.format(dev.name, zoneName))
                        self.logger.debug("API error: \n{}".format(traceback.format_exc(10)))
                        self._fireTrigger("startZoneFailed", dev.id)
                else:
                    self.logger.error("Zone number {} doesn't exist in this controller and can't be enabled.".format(action.zoneIndex))
                    self._fireTrigger("startZoneFailed", dev.id)

        ###### ALL ZONES OFF ######
        elif action.sprinklerAction == indigo.kSprinklerAction.AllZonesOff:
            data = {
                "id": dev.states["id"],
            }
            try:
                self._make_api_call(DEVICE_STOP_WATERING_URL.format(apiVersion=RACHIO_API_VERSION), request_method="put", data=data)
                self.logger.info(u'sent "{}" {}'.format(dev.name, "all zones off"))
                dev.updateStateOnServer("activeZone", 0)
            except:
                # Else log failure but do NOT update state on Indigo Server.
                self.logger.info(u'send "{}" {} failed'.format(dev.name, "all zones off"))
                self.logger.debug("API error: \n{}".format(traceback.format_exc(10)))
                self._fireTrigger("stopFailed", dev.id)

        ############################################
        # TODO: The next sprinkler actions won't currently be called because we haven't set the OverrideScheduleActions
        # property. If we wanted to hand off all scheduling to the Rachio, we would need to use these. However, their
        # current API doesn't implement enough required functionality (pause/resume, next/previous zone, etc) for us to
        # actually do that at the moment.
        ############################################
        elif action.sprinklerAction == indigo.kSprinklerAction.RunNewSchedule or \
             action.sprinklerAction == indigo.kSprinklerAction.RunPreviousSchedule or \
             action.sprinklerAction == indigo.kSprinklerAction.PauseSchedule or \
             action.sprinklerAction == indigo.kSprinklerAction.ResumeSchedule or \
             action.sprinklerAction == indigo.kSprinklerAction.StopSchedule or \
             action.sprinklerAction == indigo.kSprinklerAction.PreviousZone or \
             action.sprinklerAction == indigo.kSprinklerAction.NextZone:
            pass

    ########################################
    # General Action callback
    ########################################
    def actionControlUniversal(self, action, dev):
        ###### STATUS REQUEST ######
        if action.deviceAction == indigo.kUniversalAction.RequestStatus:
            self._next_weather_update = datetime.now()
            self._update_from_rachio()

    ########################################
    # Custom Plugin Action callbacks defined in Actions.xml
    ########################################
    def runRachioSchedule(self, pluginAction, dev):
        schedule_rule_id = pluginAction.props["scheduleId"]
        if self.throttle_next_call:
            self.logger.error("API calls have violated rate limit - next connection attempt at {:%H:%M:%S}".format(
                    self.throttle_next_call)
            )
            self._fireTrigger("startRachioScheduleFailed", dev.id)
        else:
            dev_dict = self._get_device_dict(dev.states["id"])
            if dev_dict:
                schedule_id_dict = {rule_dict["id"]: rule_dict["name"] for rule_dict in dev_dict["scheduleRules"]}
                if schedule_rule_id in schedule_id_dict.keys():
                    try:
                        data = {
                            "id": schedule_rule_id,
                        }
                        self._make_api_call(SCHEDULERULE_START_URL, request_method="put", data=data)
                        self.logger.info("Rachio schedule '{}' started".format(schedule_id_dict[schedule_rule_id]))
                        self.logger.warn("Note: frequently requesting dynamic status updates may cause failures later because of Rachio API polling limits. Use sparingly.")
                        return
                    except Exception as exc:
                        self.logger.debug("API error: \n{}".format(traceback.format_exc(10)))
                        self._fireTrigger("startRachioScheduleFailed", dev.id)
            self.logger.error("No Rachio schedule found matching action configuration - check your action.")

    ########################################
    def setSeasonalAdjustment(self, pluginAction, dev):
        try:
            if int(pluginAction.props["adjustment"]) not in range(-100, 100):
                raise Exception()
        except:
            self.logger.error("Seasonal adjustments must be specified as an integer from -100 to 100 (a percentage)")
            return
        if self.throttle_next_call:
            self.logger.error("API calls have violated rate limit - next connection attempt at {:%H:%M:%S}".format(
                    self.throttle_next_call)
            )
            self._fireTrigger("setSeasonalAdjustmentFailed", dev.id)
        else:
            schedule_rule_id = pluginAction.props["scheduleId"]
            dev_dict = self._get_device_dict(dev.states["id"])
            if dev_dict:
                schedule_id_dict = {rule_dict["id"]: rule_dict["name"] for rule_dict in dev_dict["scheduleRules"]}
                if schedule_rule_id in schedule_id_dict.keys():
                    try:
                        data = {
                            "id": schedule_rule_id,
                            "adjustment": int(pluginAction.props["adjustment"]) * .01
                        }
                        self._make_api_call(SCHEDULERULE_START_URL, request_method="put", data=data)
                        self.logger.info("Rachio seasonal adjustment set to {}%".format(pluginAction.props["adjustment"]))
                        return
                    except Exception as exc:
                        self.logger.debug("API error: \n{}".format(traceback.format_exc(10)))
                        self._fireTrigger("setSeasonalAdjustmentFailed", dev.id)
            self.logger.error("No Rachio schedule found matching action configuration - check your action.")

    ########################################
    def setStandbyMode(self, pluginAction, dev):
        try:
            data = {
                "id": dev.states["id"],
            }
            if pluginAction.props["mode"]:
                # You turn the device off to put it into standby mode
                url = DEVICE_TURN_OFF_URL.format(apiVersion=RACHIO_API_VERSION)
            else:
                # You turn the device on to take it out of standby mode
                url = DEVICE_TURN_ON_URL.format(apiVersion=RACHIO_API_VERSION)
            self._make_api_call(url, request_method="put", data=data)
            self.logger.info("Standby mode for controller '{}' turned {}".format(dev.name, "on" if pluginAction.props["mode"] else "off"))
        except Exception as exc:
            self.logger.error("Could not set standby mode - check your controller.")
            self.logger.debug("API error: \n{}".format(traceback.format_exc(10)))
            self._fireTrigger("setStandbyFailed", dev.id)

    ########################################
    # Menu callbacks defined in MenuItems.xml
    ########################################
    def toggleDebugging(self):
        if self.debug:
            self.logger.info("Turning off debug logging")
            self.pluginPrefs["showDebugInfo"] = False
        else:
            self.logger.info("Turning on debug logging")
            self.pluginPrefs["showDebugInfo"] = True
        self.debug = not self.debug

    ########################################
    def updateAllStatus(self):
        self._next_weather_update = datetime.now()
        self._update_from_rachio()
