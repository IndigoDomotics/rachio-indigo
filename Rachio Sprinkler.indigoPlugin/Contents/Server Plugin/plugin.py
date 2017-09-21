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
from datetime import datetime, timedelta
from dateutil import tz

RACHIO_API_VERSION              = "1"
RACHIO_MAX_ZONE_DURATION        = 10800
DEFAULT_API_CALL_TIMEOUT        = 5
DEFAULT_POLLING_INTERVAL        = 60   # number of seconds between each poll, default is 1 minute
DEFAULT_WEATHER_UPDATE_INTERVAL = DEFAULT_POLLING_INTERVAL * 10  # number of seconds between each forecast update, default is 10 minutes

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
        # Not currently exposed in the plugin prefs, but we can if necessary
        self.debug = pluginPrefs.get("showDebugInfo", False)
        # Not currently exposed in the plugin prefs, but we can if necessary
        self.pollingInterval = pluginPrefs.get("pollingInterval", DEFAULT_POLLING_INTERVAL)
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
        self._next_weather_update = datetime.now()
        #self._update_from_rachio()

    def _make_api_call(self, url, request_method="get", data=None):
        return_val = None
        try:
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
        except Exception as exc:
            self.logger.error("Connection to Rachio API server failed with exception: {}. Check the log file for full details.".format(exc.__class__.__name__))
            self.logger.debug("Connection to Rachio API server failed with exception:\n{}".format(traceback.format_exc(10)))
            raise exc

    def _get_device_dict(self, id):
        dev_list = [dev_dict for dev_dict in self.person["devices"] if dev_dict["id"] == id]
        if len(dev_list):
            return dev_list[0]
        else:
            return None

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
                        return
                try:
                    reply_dict = self._make_api_call(PERSON_URL.format(apiVersion=RACHIO_API_VERSION, personId=self.person_id))
                except:
                    self.logger.error("Error getting user data from Rachio via API.")
                    return
                self.person = reply_dict
                self.rachio_devices = self.person["devices"]
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

    def _update_forecast_data(self, dev):
        if datetime.now() >= self._next_weather_update:
            try:
                units = dev.pluginProps["units"]
                reply_dict = self._make_api_call(DEVICE_GET_FORECAST_URL.format(apiVersion=RACHIO_API_VERSION,
                                                                                deviceId=dev.states["id"],
                                                                                units=units))
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
                        if k in FORECAST_FIELDS_USED:
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

    ########################################
    def startup(self):
        pass

    def shutdown(self):
        pass

    def runConcurrentThread(self):
        self.logger.debug("Starting concurrent tread")
        try:
            # Polling - if we ever implement a webhook catcher from the Rachio API we would no longer need to poll.
            # But since that would currently require an IWS handler and some way to communicate between the two we'll
            # save that for later when the two APIs are integrated.
            while True:
                try:
                    self._update_from_rachio()
                except:
                    pass
                self.sleep(self.pollingInterval)

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
    # Valication callbacks
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
                    raise
            except:
                errorsDict["adjustment"] = "Must be an integer from -100 to 100 (a percentage)"
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
        else:
            self._next_weather_update = datetime.now()
            self._update_from_rachio()

    def deviceStopComm(self, dev):
        self._next_weather_update = datetime.now()
        self._update_from_rachio()

    ########################################
    # Sprinkler Control Action callback
    ########################################
    def actionControlSprinkler(self, action, dev):
        ###### ZONE ON ######
        if action.sprinklerAction == indigo.kSprinklerAction.ZoneOn:
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
                    # Else log failure but do NOT update state on Indigo Server.
                    self.logger.error(u'send "{} - {}" on failed'.format(dev.name, zoneName))
            else:
                self.logger.error("Zone number {} doesn't exist in this controller and can't be enabled.".format(action.zoneIndex))
                # FIXME: do we want to send a subscription notification here on failure?

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
                # FIXME: do we want to send a subscription notification here on failure?

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
    ######################
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
                    return
                except Exception as exc:
                    pass
        self.logger.error("No Rachio schedule found matching action configuration - check your action.")

    def setSeasonalAdjustment(self, pluginAction, dev):
        try:
            if int(pluginAction.props["adjustment"]) not in range(-100, 100):
                raise
        except:
            self.logger.error("Seasonal adjustments must be specified as an integer from -100 to 100 (a percentage)")
            return
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
                    pass
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

    def updateAllStatus(self):
        self._next_weather_update = datetime.now()
        self._update_from_rachio()
