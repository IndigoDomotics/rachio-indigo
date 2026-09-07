#! /usr/bin/env python
# -*- coding: utf-8 -*-
####################
# Copyright (c) 2014, Perceptive Automation, LLC. All rights reserved.
# http://www.indigodomo.com

import indigo
import requests
import json
import copy
import traceback
from operator import itemgetter
from datetime import datetime, timedelta, time
from dateutil import tz
from distutils.version import LooseVersion
import random
# 2026-08-17 (Ham + Claude): aliased because `time` above is datetime.time
# (time-of-day), not the time module -- needed for a real UTC epoch-ms
# clock, see _shouldAcceptZoneUpdate.
import time as time_module

RACHIO_API_VERSION = "1"
RACHIO_MAX_ZONE_DURATION = 10800
DEFAULT_API_CALL_TIMEOUT = 5  # number of seconds after which we time out any network calls
MINIMUM_POLLING_INTERVAL = 3  # number of minutes between each poll, default is 3 (changed 2/27/2018 to help avoid throttling)
DEFAULT_WEATHER_UPDATE_INTERVAL = 10  # number of minutes between each forecast update, default is 10
DEFAULT_PERSON_UPDATE_INTERVAL = 15  # 2026-09-01 (Ham + Claude): minutes between full account/person refreshes (D — throttle person-data off the 3-min poll)
THROTTLE_LIMIT_TIMER = 61  # number of minutes to wait if we've received a throttle error before doing any API calls
FORECAST_UPDATE_INTERVAL = 60  # minutes between forecast updates

API_URL = "https://api.rach.io/{apiVersion}/public/"
PERSON_URL = API_URL + "person/{personId}"
PERSON_INFO_URL = PERSON_URL.format(apiVersion=RACHIO_API_VERSION, personId="info")
DEVICE_BASE_URL = API_URL + "device/"
DEVICE_GET_URL = DEVICE_BASE_URL + "{deviceId}"
DEVICE_CURRENT_SCHEDULE_URL = DEVICE_GET_URL + "/current_schedule"
DEVICE_STOP_WATERING_URL = DEVICE_BASE_URL + "stop_water"
DEVICE_TURN_OFF_URL = DEVICE_BASE_URL + "off"
DEVICE_TURN_ON_URL = DEVICE_BASE_URL + "on"
DEVICE_GET_FORECAST_URL = DEVICE_GET_URL + "/forecast?units={units}"
ZONE_URL = API_URL + "zone/"
ZONE_START_URL = ZONE_URL + "start"
SCHEDULERULE_URL = API_URL + "schedulerule/{scheduleRuleId}"
SCHEDULERULE_START_URL = SCHEDULERULE_URL.format(apiVersion=RACHIO_API_VERSION, scheduleRuleId="start")
SCHEDULERULE_SEASONAL_ADJ_URL = SCHEDULERULE_URL.format(apiVersion=RACHIO_API_VERSION,
                                                        scheduleRuleId="seasonal_adjustment")

FORECAST_DAYS_SUPPORTED = 14
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
    time_utc = datetime.utcfromtimestamp(timestamp / 1000)
    time_utc_gmt = time_utc.replace(tzinfo=from_zone)
    return time_utc_gmt.astimezone(to_zone)


def get_key_from_dict(a_key, a_dict):
    try:
        return a_dict[a_key]
    except KeyError:
        return "unavailable from API"
    except (Exception,):
        return "unknown error"


################################################################################
class Plugin(indigo.PluginBase):
    ########################################
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super(Plugin, self).__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        # Used to control when to show connection errors (vs just repeated retries)
        self._displayed_connection_error = False
        self.pluginId = pluginId
        self.debug = pluginPrefs.get("showDebugInfo", False)
        self.pollingInterval = int(pluginPrefs.get("pollingInterval", MINIMUM_POLLING_INTERVAL))
        self.timeout = int(pluginPrefs.get("apiTimeout", DEFAULT_API_CALL_TIMEOUT))

        self.unused_devices = {}
        self.access_token = pluginPrefs.get("accessToken", None)
        self.person_id = pluginPrefs.get("personId", None)
        self.maxZoneRunTime = int(pluginPrefs.get("maxZoneRunTime", RACHIO_MAX_ZONE_DURATION))

        if self.access_token:
            self.headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.access_token}"
            }
        else:
            self.logger.warn("You must specify your API token in the plugin's config before the plugin can be used.")
            self.headers = None
        self.triggerDict = {}
        self._next_weather_update = datetime.now()
        # 2026-09-01 (Ham + Claude): D — throttle the full account/person fetch off the
        # 3-min poll. self.person caches the last account snapshot; between refreshes the
        # poll reuses it for device metadata while STILL fetching each controller's live
        # current_schedule (running state) + weather (own timer). Person data (zones,
        # names, location, schedule mode) rarely changes, so re-fetching the heavy
        # account call every 3 min was wasted budget — refresh it every
        # personUpdateInterval minutes instead (~20/hr → ~4/hr).
        self.person = None
        self.rachio_devices = None
        self._next_person_update = datetime.now()  # first poll fetches
        self.personUpdateInterval = int(pluginPrefs.get("personUpdateInterval", DEFAULT_PERSON_UPDATE_INTERVAL))
        # 2026-09-01 (Ham + Claude): restore an active rate-limit backoff across a
        # plugin reload. throttle_next_call was in-memory only, so a reload forgot it
        # and immediately re-hammered a still-throttled API — each 429 pushes the reset
        # further out (self-inflicted lockout extension). Persist it and honor on start.
        self.throttle_next_call = None
        try:
            _saved_throttle_ts = float(pluginPrefs.get("throttle_next_call_ts", 0) or 0)
            if _saved_throttle_ts:
                _resume_at = datetime.fromtimestamp(_saved_throttle_ts)
                if _resume_at > datetime.now():
                    self.throttle_next_call = _resume_at
                    self.logger.warning(
                        f"Rachio API still in rate-limit backoff from before reload — "
                        f"holding all calls until {self.throttle_next_call:%H:%M:%S}.")
        except Exception:
            self.throttle_next_call = None
        self.webhook_url = None
        self.use_webhooks = False
        # 2026-08-17 (Ham + Claude), real incident: webhooks (real-time,
        # push-based) and the poll in _update_from_rachio (an HTTP round-
        # trip that can be in flight for several seconds) both write
        # activeZone/activeZone.ui to the same device states with no
        # ordering guarantee -- confirmed live: a poll response that
        # started before a newer webhook already advanced the zone can
        # still complete afterward and overwrite the correct value with a
        # stale one. Observed consequence in the consuming plugin (SFM):
        # one such stale write persisted long enough to defeat SFM's own
        # debounce and get committed as a real (backward) zone transition,
        # merging one zone's water into another's tally. Tracks, per
        # device id, the zoneStartDate (epoch ms) of whatever zone is
        # currently believed live -- see _shouldAcceptZoneUpdate.
        self._last_zone_start_ms = {}

    ########################################
    # Internal helper methods
    ########################################

    def _make_api_call(self, url, request_method="get", data=None):
        try:
            if self.throttle_next_call:
                if self.throttle_next_call > datetime.now():
                    # Too soon, raise exception
                    raise ThrottleDelayError(
                        f"API calls have violated rate limit - next connection attempt at {self.throttle_next_call:%H:%M:%S}")
                else:
                    self.throttle_next_call = None
                    self.pluginPrefs["throttle_next_call_ts"] = 0   # 2026-09-01 (Ham + Claude): clear persisted backoff
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
                self.logger.error(
                    "Unable to contact device - the controller may be offline. Will continue to retry silently.")
                self._displayed_connection_error = True
            raise exc
        except requests.exceptions.Timeout as exc:
            if not self._displayed_connection_error:
                self.logger.error("Connection to Rachio API server failed. Will continue to retry silently.")
                self._displayed_connection_error = True
            raise exc
        except requests.exceptions.HTTPError as exc:
            if exc.response.status_code == 429:
                # We've hit the throttle limit - we need to back off on all requests for some period of time
                self.throttle_next_call = datetime.now() + timedelta(minutes=THROTTLE_LIMIT_TIMER)
                self.pluginPrefs["throttle_next_call_ts"] = self.throttle_next_call.timestamp()  # 2026-09-01 (Ham + Claude): survive reloads
                self._fireTrigger("rateLimitExceeded")
            raise exc
        except ThrottleDelayError as exc:
            self.logger.error(str(exc))
            self.logger.debug(f"{str(exc)}:\n{traceback.format_exc(10)}")
            raise exc
        except Exception as exc:
            self.logger.error(
                f"Connection to Rachio API server failed with exception: {exc.__class__.__name__}. Check the log file for full details.")
            self.logger.debug(
                f"Connection to Rachio API server failed with exception:\n{traceback.format_exc(10)}")
            raise exc

    ########################################
    def _get_device_dict(self, dev_id):
        dev_list = [dev_dict for dev_dict in self.person["devices"] if dev_dict["id"] == dev_id]
        if len(dev_list):
            return dev_list[0]
        else:
            return None

    ########################################
    def _get_zone_dict(self, dev_id, zoneNumber):
        dev_dict = self._get_device_dict(dev_id)
        if dev_dict:
            zone_list = [zone_dict for zone_dict in dev_dict["zones"] if zone_dict["zoneNumber"] == zoneNumber]
            if len(zone_list):
                return zone_list[0]
        return None

    ########################################
    def _shouldAcceptZoneUpdate(self, devId, new_start_ms):
        """2026-08-17 (Ham + Claude): guards against the poll/webhook write
        race described where self._last_zone_start_ms is set up. new_start_ms
        is the zoneStartDate (epoch ms) the CALLER wants to write for devId.
        Returns True (accept, and record new_start_ms as the new high-water
        mark) unless new_start_ms is nonzero AND strictly older than the
        most recent zoneStartDate already recorded for this device -- that
        specific combination means this write is for a zone that's already
        been superseded by fresher data, almost always a slow poll response
        landing after a newer webhook already moved things forward. A
        missing/zero new_start_ms (some responses may not carry one) is
        always accepted -- there's nothing to compare, and refusing every
        such write would break normal operation, not just the race case."""
        if not new_start_ms:
            return True
        last_known = self._last_zone_start_ms.get(devId, 0)
        if last_known and new_start_ms < last_known:
            return False
        self._last_zone_start_ms[devId] = new_start_ms
        return True

    ########################################
    def _update_from_rachio(self):
        self.logger.debug("_update_from_rachio")
        try:
            if self.access_token:
                if not self.person_id:
                    try:
                        reply = self._make_api_call(PERSON_INFO_URL)
                        self.person_id = reply["id"]
                        self.pluginPrefs["personId"] = self.person_id
                    except Exception as exc:
                        self.logger.error("Error getting user ID from Rachio via API.")
                        self.logger.debug(f"API error: \n{traceback.format_exc(10)}")
                        self._fireTrigger("personCall")
                        return
                # 2026-09-01 (Ham + Claude): D — only re-fetch the full account/person
                # snapshot when the throttle window has elapsed (or we have no cached
                # snapshot yet). Between refreshes we reuse self.person for device
                # metadata; the per-device current_schedule (running state) and weather
                # below still run every poll, so live state stays fresh while the heavy
                # account call drops from ~20/hr to ~4/hr.
                if self.person is None or datetime.now() >= self._next_person_update:
                    try:
                        reply_dict = self._make_api_call(
                            PERSON_URL.format(apiVersion=RACHIO_API_VERSION, personId=self.person_id))
                        self.person = reply_dict
                        self.rachio_devices = self.person["devices"]
                        self._next_person_update = datetime.now() + timedelta(minutes=self.personUpdateInterval)
                    except Exception as exc:
                        self.logger.error("Error getting user data from Rachio via API.")
                        self.logger.debug(f"API error: \n{traceback.format_exc(10)}")
                        self._fireTrigger("personInfoCall")
                        if self.person is None:
                            return  # no cached snapshot to fall back on
                        # else: proceed this cycle with the cached account snapshot

                current_device_uuids = [s.states["id"] for s in indigo.devices.iter(filter="self")]
                self.unused_devices = {dev_dict["id"]: dev_dict for dev_dict in self.person["devices"] if
                                       dev_dict["id"] not in current_device_uuids}
                self.defined_devices = {dev_dict["id"]: dev_dict for dev_dict in self.person["devices"] if
                                        dev_dict["id"] in current_device_uuids}
                defined_devices = [dev_dict for dev_dict in self.person["devices"] if
                                   dev_dict["id"] in current_device_uuids]

                for dev in [s for s in indigo.devices.iter(filter="self") if s.enabled]:

                    for dev_dict in defined_devices:

                        # Find the matching update dict for the device
                        if dev_dict["id"] == dev.states["id"]:
                            # Update any changed information for the device - we only look at the data that may change
                            # as part of operation - anything that's fixed (serial number, etc. gets set once when the
                            # device is created or when the user replaces the controller.
                            update_list = []

                            # "status" is ONLINE or OFFLINE - if the latter it's unplugged or otherwise can't communicate with the cloud
                            # note: it often takes a REALLY long time for the API to return OFFLINE, and sometimes it never does.
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
                            update_list.append({"key": "paused", "value": get_key_from_dict("paused", dev_dict)})

                            # Update location-based stuff
                            try:
                                if dev_dict["latitude"] != dev.states["latitude"]:
                                    update_list.append({"key": "latitude", "value": dev_dict["latitude"]})
                            except (Exception,):
                                self.logger.debug(u"The 'latitude' field wasn't returned by the API.")
                                update_list.append({"key": "latitude", "value": "unavailable from API"})
                            try:
                                if dev_dict["longitude"] != dev.states["longitude"]:
                                    update_list.append({"key": "longitude", "value": dev_dict["longitude"]})
                            except (Exception,):
                                self.logger.debug(u"The 'longitude' field wasn't returned by the API.")
                                update_list.append({"key": "longitude", "value": "unavailable from API"})
                            try:
                                if dev_dict["timeZone"] != dev.states["timeZone"]:
                                    update_list.append({"key": "timeZone", "value": dev_dict["timeZone"]})
                            except (Exception,):
                                self.logger.debug(u"The 'timeZone' field wasn't returned by the API.")
                                update_list.append({"key": "timeZone", "value": "unavailable from API"})
                            try:
                                if dev_dict["utcOffset"] != dev.states["utcOffset"]:
                                    update_list.append({"key": "utcOffset", "value": dev_dict["utcOffset"]})
                            except (Exception,):
                                self.logger.debug(u"The 'utcOffset' field wasn't returned by the API.")
                                update_list.append({"key": "utcOffset", "value": "unavailable from API"})

                            activeScheduleName = None
                            # Get the current schedule for the device - it will tell us if it's running or not
                            try:
                                current_schedule_dict = self._make_api_call(
                                    DEVICE_CURRENT_SCHEDULE_URL.format(apiVersion=RACHIO_API_VERSION,
                                                                       deviceId=dev.states["id"]))
                                if len(current_schedule_dict):
                                    # Diagnostic dump of Rachio's raw current-schedule response
                                    # (carries per-zone duration data). GATED behind debug logging
                                    # (Toggle Debugging menu) so it's on-demand, quiet otherwise.
                                    if self.debug:
                                        self.logger.info(
                                            f"{dev.name}: [DIAG] raw current_schedule_dict = "
                                            f"{current_schedule_dict}")
                                    # Something is running, so we need to figure out if it's a manual or automatic schedule and
                                    # if it's automatic (a Rachio schedule) then we need to get the name of that schedule
                                    #
                                    # 2026-08-17 (Ham + Claude), real incident: this API call can
                                    # take several seconds to complete. If a webhook for a NEWER
                                    # zone transition arrives and updates activeZone while this
                                    # call was already in flight, this response is now stale --
                                    # writing it unconditionally would overwrite the correct,
                                    # newer value with an old one. Confirmed live in the
                                    # consuming plugin (SFM): exactly this caused a real backward
                                    # zone-tracking error. _shouldAcceptZoneUpdate compares this
                                    # response's own zoneStartDate against the most recent one
                                    # already seen for this device and skips the write (only for
                                    # activeZone/activeZoneDuration/activeZoneStartDate -- nothing
                                    # else in this update cycle) if this response is for an
                                    # already-superseded zone.
                                    if self._shouldAcceptZoneUpdate(
                                            dev.id, current_schedule_dict.get("zoneStartDate", 0)):
                                        update_list.append(
                                            {"key": "activeZone",
                                             "value": current_schedule_dict["zoneNumber"]})
                                        # 2026-08-17 (Ham + Claude): current_schedule_dict already
                                        # carries the active zone's planned duration and start time
                                        # (zoneDuration seconds, zoneStartDate epoch ms) -- confirmed
                                        # live for an AUTOMATIC (cron-triggered) run. Exposing both as
                                        # real states so SFM can attribute flow by timestamp against
                                        # Rachio's own authoritative schedule instead of reacting to
                                        # every activeZone change immediately (the source of the
                                        # flapping/misattribution issues found 2026-08-17). Using
                                        # .get() since it's still unconfirmed whether a MANUALLY-
                                        # started run of a real schedule includes these same keys.
                                        update_list.append(
                                            {"key": "activeZoneDuration",
                                             "value": current_schedule_dict.get("zoneDuration", 0)})
                                        # 2026-08-29 (Ham + Claude): store epoch SECONDS, not the raw
                                        # epoch MILLISECONDS Rachio returns. The SQL Logger's Postgres
                                        # backend types state columns as 32-bit integer (max ~2.1e9);
                                        # an epoch-ms value (~1.79e12) overflows and errors on every
                                        # write during a run. Epoch seconds (~1.79e9) fits. // 1000
                                        # keeps 0 (missing key) as 0.
                                        update_list.append(
                                            {"key": "activeZoneStartDate",
                                             "value": current_schedule_dict.get("zoneStartDate", 0) // 1000})
                                    else:
                                        self.logger.debug(
                                            f"{dev.name}: skipping stale activeZone write -- "
                                            f"this poll response's zone already superseded by "
                                            f"newer data.")
                                    # 2026-08-17 (Ham + Claude): same response also carries the
                                    # WHOLE schedule's own duration/startDate (not just the
                                    # current zone's) -- Ham's use case: the all-zones-off
                                    # watchdog (SFM's RACHIO_ALL_OFF_DEBOUNCE_SECONDS) currently
                                    # waits a blind 90s any time Rachio reports "all zones off"
                                    # mid-run, purely because that signal alone can't distinguish
                                    # a genuine finish from a cloud-side flap. Knowing the
                                    # schedule's own expected end time lets SFM corroborate:
                                    # deliberately NOT cleared in the "no active schedule" branch
                                    # below -- these must still be readable for a little while
                                    # AFTER the schedule ends, which is exactly when SFM needs to
                                    # compare against them.
                                    update_list.append(
                                        {"key": "activeScheduleDuration",
                                         "value": current_schedule_dict.get("duration", 0)})
                                    # 2026-08-29 (Ham + Claude): epoch SECONDS, not ms — see the
                                    # activeZoneStartDate note above (32-bit Postgres overflow).
                                    update_list.append(
                                        {"key": "activeScheduleStartDate",
                                         "value": current_schedule_dict.get("startDate", 0) // 1000})
                                    if current_schedule_dict["type"] == "AUTOMATIC":
                                        schedule_detail_dict = self._make_api_call(
                                            SCHEDULERULE_URL.format(apiVersion=RACHIO_API_VERSION,
                                                                    scheduleRuleId=current_schedule_dict[
                                                                        "scheduleRuleId"]))
                                        # Diagnostic dump of Rachio's raw schedule-detail response
                                        # (only "name" is normally used, but it carries per-zone
                                        # durations). GATED behind debug logging (Toggle Debugging
                                        # menu) so it's on-demand, quiet otherwise.
                                        if self.debug:
                                            self.logger.info(
                                                f"{dev.name}: [DIAG] raw schedule_detail_dict = "
                                                f"{schedule_detail_dict}")
                                        update_list.append(
                                            {"key": "activeSchedule", "value": schedule_detail_dict["name"]})
                                        activeScheduleName = schedule_detail_dict["name"]
                                        # 2026-08-29 (Ham + Claude): compose a human-readable per-zone
                                        # PLAN (zone number + name + planned minutes, in run order) by
                                        # joining this response's per-zone durations to the device's zone
                                        # list. Exposed as activeSchedulePlan so SFM can text it at
                                        # schedule start. AUTOMATIC schedules only (manual runs carry no
                                        # scheduleRuleId → no per-zone plan).
                                        try:
                                            dd = self._get_device_dict(dev.states["id"])
                                            id_map = {}
                                            if dd:
                                                for z in dd.get("zones", []):
                                                    if z.get("id") is not None:
                                                        id_map[z["id"]] = (z.get("zoneNumber"), z.get("name"))
                                            parts = []
                                            for sz in sorted(schedule_detail_dict.get("zones", []),
                                                             key=lambda x: x.get("sortOrder", 0)):
                                                num, nm = id_map.get(sz.get("zoneId"), (None, None))
                                                mins = round(sz.get("duration", 0) / 60.0, 1)
                                                label = (f"Zone {num} {nm}" if nm
                                                         else f"Zone (order {sz.get('sortOrder', '?')})")
                                                parts.append(f"{label}: {mins:g} min")
                                            sched_plan = "; ".join(parts)
                                        except Exception as exc:
                                            sched_plan = ""
                                            self.logger.debug(f"{dev.name}: schedule-plan build failed: {exc}")
                                        update_list.append({"key": "activeSchedulePlan", "value": sched_plan})

                                    else:
                                        update_list.append(
                                            {"key": "activeSchedule", "value": current_schedule_dict["type"].title()})
                                        activeScheduleName = current_schedule_dict["type"].title()
                                        # Manual run — no per-zone plan; clear so a stale AUTOMATIC plan
                                        # can't be mis-texted for a hand-started run.
                                        update_list.append({"key": "activeSchedulePlan", "value": ""})
                                else:
                                    update_list.append({"key": "activeSchedule", "value": "No active schedule"})
                                    # Show no zones active
                                    update_list.append({"key": "activeZone", "value": 0})
                                    update_list.append({"key": "activeZoneDuration", "value": 0})
                                    update_list.append({"key": "activeZoneStartDate", "value": 0})
                                    update_list.append({"key": "activeSchedulePlan", "value": ""})
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
                            # 2026-09-01 (Ham + Claude): only write props when they actually
                            # changed. replacePluginPropsOnServer restarts device comm on every
                            # call (no didDeviceCommPropertyChange override), so the unconditional
                            # per-poll write caused a device-restart storm + didDeviceCommProperty-
                            # Change log spam. Zone names/counts rarely change between polls.
                            if dict(props) != dict(dev.pluginProps):
                                dev.replacePluginPropsOnServer(props)

                    # Update the forecasts

                    self._update_forecast_data(dev)

            else:
                self.logger.warn(
                    "You must specify your API token in the plugin's config before the plugin can be used.")
        except Exception as exc:
            self.logger.error("Unknown error:\n{}".format(traceback.format_exc(10)))

    ########################################
    def _update_forecast_data(self, dev):
        if datetime.now() >= self._next_weather_update:
            try:
                units = dev.pluginProps["units"]
                reply_dict = self._make_api_call(
                    DEVICE_GET_FORECAST_URL.format(apiVersion=RACHIO_API_VERSION, deviceId=dev.states["id"],
                                                   units=units))
                current_conditions = reply_dict.get("current", None)
                if not current_conditions:
                    self.logger.warning("No current conditions returned from API")
                    return
                
                state_update_list = []
                for k, v in current_conditions.items():
                    if k in FORECAST_FIELDS_USED:
                        update_dict = {"key": "current_{}".format(k), "value": v}
                        if "decimalPlaces" in FORECAST_FIELDS_USED[k]:
                            update_dict["decimalPlaces"] = FORECAST_FIELDS_USED[k].split(":")[1] # noqa PyCharm error
                        elif "percentage" in FORECAST_FIELDS_USED[k]:
                            update_dict["value"] = v * 100
                            update_dict["decimalPlaces"] = 0
                            update_dict["uiValue"] = u"{}%".format(update_dict["value"])
                        if u"currentTemperature" in k or u"dewPoint" in k:
                            update_dict["uiValue"] = u"{} °{}".format(update_dict["value"],
                                                                      u"F" if units == "US" else u"C")
                        state_update_list.append(update_dict)
                        if k == u"currentTemperature":
                            pass
                forecasts_sorted = sorted(reply_dict["forecast"], key=itemgetter("time"))
                for count, forecast in enumerate(forecasts_sorted):
                    # We currently only support forecasts for 14 days including today, ignore more
                    if count < FORECAST_DAYS_SUPPORTED:
                        for k, v in forecast.items():
                            # For some strange reason, the API started returning currentTemperature for forecast days, which
                            # makes no sense because it's a FORECAST (nothing current about it). So, we'll just ignore it.
                            if k in FORECAST_FIELDS_USED and k != "currentTemperature":
                                update_dict = {"key": "t{}forecast_{}".format(count, k), "value": v}
                                if "decimalPlaces" in FORECAST_FIELDS_USED[k]:
                                    update_dict["decimalPlaces"] = FORECAST_FIELDS_USED[k].split(":")[1]  # noqa PyCharm error
                                elif "percentage" in FORECAST_FIELDS_USED[k]:
                                    update_dict["value"] = v * 100
                                    update_dict["decimalPlaces"] = 0
                                    update_dict["uiValue"] = u"{}%".format(update_dict["value"])
                                if "temperature" in k or "dewPoint" in k:
                                    update_dict["uiValue"] = u"{} °{}".format(update_dict["value"],
                                                                              u"F" if units == "US" else u"C")
                                state_update_list.append(update_dict)
                dev.updateStatesOnServer(state_update_list)
                # 2026-09-01 (Ham + Claude): was timedelta(SECONDS=...) — but
                # DEFAULT_WEATHER_UPDATE_INTERVAL is documented and intended as MINUTES
                # (10). As seconds, the forecast re-fetch was eligible ~every poll, so a
                # full 14-day forecast was pulled on essentially every 3-min cycle (and
                # every forced-weather status request). MINUTES restores the intent.
                self._next_weather_update = datetime.now() + timedelta(minutes=DEFAULT_WEATHER_UPDATE_INTERVAL)
            except Exception as exc:
                self.logger.error("Error getting forecast data from Rachio via API.")
                self.logger.debug("API error: \n{}".format(traceback.format_exc(10)))
                self._fireTrigger("forecastCall")

    ########################################
    # startup, concurrent thread, and shutdown methods
    ########################################
    def startup(self):

        self.logger.info("Rachio Sprinklers Started")

        self.use_webhooks = bool(self.pluginPrefs.get("useWebhooks", False))
        if not self.use_webhooks:
            return

        # Test here to see if Reflector webhook is available, get reflector name, etc.
        reflectorURL = indigo.server.getReflectorURL()
        reflector_api_key = self.pluginPrefs.get("reflector_api_key", None)
        if not reflector_api_key:
            self.logger.warning("Unable to set up Rachio webhooks - no reflector API key")
            self.use_webhooks = False

        self.webhook_url = f"{reflectorURL}/message/{self.pluginId}/webhook?api_key={reflector_api_key}"
        self.logger.debug(f"Using Reflector, webhook_url: {self.webhook_url}")

    ########################################
    def shutdown(self):
        self.logger.info("Rachio Sprinklers Stopped")
        pass

    ########################################
    def runConcurrentThread(self):
        self.logger.debug("Starting concurrent thread")
        try:
            # We only need to poll for forecast data, if webhooks are enabled

            while True:
                try:
                    self._update_from_rachio()
                except (Exception,):
                    pass
                self.sleep(self.pollingInterval * 60)

        except self.StopThread:
            self.logger.debug("Received StopThread")

    ########################################

    def reflector_handler(self, action, dev=None, callerWaitingForResult=None):
        self.logger.debug(f"reflector_handler: {action.props}")
        self.webHook_handler(json.loads(action.props['request_body']))
        return "200"

    def webHook_handler(self, payload):
        self.logger.debug(f"webHook_handler: {payload}")

        self.logger.info(
            f"webHook received, {payload.get('category', '')}/{payload.get('type', '')}/{payload.get('subType', '')}/{payload.get('eventType', '')}: {payload.get('summary', '')}")

        # Find the Indigo device for the Rachio Device
        for dev in indigo.devices.iter(filter="self"):
            if dev.pluginProps['id'] == payload['deviceId']:
                break
        else:
            self.logger.debug(f"webHook_handler: No matching Indigo device for Rachio deviceId '{payload['deviceId']}'")
            return

        eventType = payload.get("eventType", "")

        # 2026-08-17 (Ham + Claude): webhooks are real-time/push-based --
        # every zone-run event here is, by construction, at least as fresh
        # as anything a concurrently in-flight poll could be reporting.
        # Bumping the same freshness marker _shouldAcceptZoneUpdate reads
        # (using "now" as the timestamp, since these payloads don't carry
        # their own zoneStartDate) means a poll response that started
        # before this webhook but completes after it will correctly be
        # recognized as stale and rejected, even if that poll's own
        # zoneStartDate looked newer than whatever the LAST POLL had
        # recorded -- without this, the guard's baseline would only ever
        # advance on polls, and could still be fooled by a stale poll that
        # simply hadn't been superseded by a newer POLL yet.
        if eventType in ('DEVICE_ZONE_RUN_STARTED_EVENT', 'DEVICE_ZONE_RUN_STOPPED_EVENT',
                          'DEVICE_ZONE_RUN_COMPLETED_EVENT'):
            self._last_zone_start_ms[dev.id] = time_module.time() * 1000

        if eventType == 'DEVICE_ZONE_RUN_STARTED_EVENT':
            dev.updateStateOnServer("activeZone", payload['zoneNumber'])
            self.logger.info(f"{dev.name}: Zone '{payload['zoneName']}' Started")

        elif eventType == 'DEVICE_ZONE_RUN_STOPPED_EVENT':
            dev.updateStateOnServer("activeZone", 0)
            self.logger.info(f"{dev.name}: Zone '{payload['zoneName']}' Stopped")

        elif eventType == 'DEVICE_ZONE_RUN_COMPLETED_EVENT':
            dev.updateStateOnServer("activeZone", 0)
            self.logger.info(f"{dev.name}: Zone '{payload['zoneName']}' Completed")

        elif eventType == 'SCHEDULE_STARTED_EVENT':
            dev.updateStateOnServer("activeSchedule", payload['scheduleName'])
            self.logger.info(f"{dev.name}: Schedule '{payload['scheduleName']}' Started")

        elif eventType == 'SCHEDULE_STOPPED_EVENT':
            dev.updateStateOnServer("activeSchedule", payload['scheduleName'])
            self.logger.info(f"{dev.name}: Schedule '{payload['scheduleName']}' Stopped")

        elif eventType == 'SCHEDULE_COMPLETED_EVENT':
            dev.updateStateOnServer("activeSchedule", "No active schedule")
            self.logger.info(f"{dev.name}: Schedule '{payload['scheduleName']}' Completed")

        else:
            self.logger.info(f"{dev.name}: Unknown eventType '{eventType}'")

    ########################################
    # Dialog list callbacks
    ########################################
    def availableControllers(self, dev_filter="", valuesDict=None, typeId="", targetId=0):
        self.logger.debug(f"availableControllers {self.unused_devices}")
        controller_list = [(dev_id, dev_dict['name']) for dev_id, dev_dict in self.unused_devices.items()]
        dev = indigo.devices.get(targetId, None)
        if dev and dev.configured:
            dev_dict = self._get_device_dict(dev.states["id"])
            controller_list.append((dev.states["id"], dev_dict["name"]))
        return controller_list

    ########################################
    def availableSchedules(self, dev_filter="", valuesDict=None, typeId="", targetId=0):
        schedule_list = []
        dev = indigo.devices.get(targetId, None)
        if dev:
            dev_dict = self._get_device_dict(dev.states["id"])
            schedule_list = [(rule_dict["id"], rule_dict['name']) for rule_dict in dev_dict["scheduleRules"]]
        return schedule_list

    ########################################
    def sprinklerList(self, dev_filter="", valuesDict=None, typeId="", targetId=0):
        self.logger.threaddebug(f"sprinklerList")
        return [(s.id, s.name) for s in indigo.devices.iter(filter="self")]

    ########################################
    # Validation callbacks
    ########################################
    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        self.logger.threaddebug(f"validateDeviceConfigUi")
        if devId:
            dev = indigo.devices[devId]
            if dev.pluginProps.get("id", None) != valuesDict["id"]:
                valuesDict["configured"] = False
        else:
            valuesDict["configured"] = False
        return True, valuesDict

    ########################################
    def validateActionConfigUi(self, valuesDict, typeId, devId):
        self.logger.threaddebug(f"validateActionConfigUi")
        errorsDict = indigo.Dict()
        if typeId == "setSeasonalAdjustment":
            try:
                if int(valuesDict["adjustment"]) not in range(-100, 100):
                    raise Exception()
            except (Exception,):
                errorsDict["adjustment"] = "Must be an integer from -100 to 100 (a percentage)"
        if len(errorsDict):
            return False, valuesDict, errorsDict
        return True, valuesDict

    ########################################
    def validateEventConfigUi(self, valuesDict, typeId, devId):
        self.logger.threaddebug(f"validateEventConfigUi")
        errorsDict = indigo.Dict()
        if typeId == "sprinklerError":
            if valuesDict["id"] == "":
                errorsDict["id"] = "You must select a Rachio Sprinkler device."
        if len(errorsDict):
            return False, valuesDict, errorsDict
        return True, valuesDict

    ########################################
    def validatePrefsConfigUi(self, valuesDict):
        self.logger.threaddebug(f"validatePrefsConfigUi")
        errorsDict = indigo.Dict()
        try:
            if int(valuesDict['pollingInterval']) < 3:
                raise Exception()
        except (Exception,):
            errorsDict["pollingInterval"] = "Must be a number greater than or equal to 3 (minutes)."
        if len(errorsDict):
            return False, valuesDict, errorsDict
        return True, valuesDict

    ########################################
    # General device callbacks
    ########################################
    def didDeviceCommPropertyChange(self, origDev, newDev):
        self.logger.threaddebug(f"didDeviceCommPropertyChange")
        return True if origDev.states["id"] != newDev.states["id"] else False

    ########################################
    def deviceStartComm(self, dev):
        # 2026-08-17 (Ham + Claude): new States (activeZoneDuration,
        # activeZoneStartDate) added to Devices.xml don't retroactively apply
        # to an already-existing device instance -- Indigo keeps using the
        # state list it cached when the device was created/last edited until
        # explicitly told to refresh it. Confirmed live: a plugin restart
        # alone was not enough, updateStatesOnServer for the two new keys
        # silently had nothing to write to. This call re-syncs the device's
        # state list against the current Devices.xml on every plugin start.
        dev.stateListOrDisplayStateIdChanged()
        if not dev.pluginProps["configured"]:
            # Get the full device info and update the newly created device
            dev_dict = self.unused_devices.get(dev.pluginProps["id"], None)
            if dev_dict:
                # Update all the states here
                update_list = [{"key": "id", "value": dev_dict["id"]},
                               {"key": "address", "value": get_key_from_dict("macAddress", dev_dict)},
                               {"key": "model", "value": get_key_from_dict("model", dev_dict)},
                               {"key": "serialNumber", "value": get_key_from_dict("serialNumber", dev_dict)},
                               {"key": "latitude", "value": get_key_from_dict("latitude", dev_dict)},
                               {"key": "longitude", "value": get_key_from_dict("longitude", dev_dict)},
                               {"key": "name", "value": get_key_from_dict("name", dev_dict)},
                               {"key": "inStandbyMode",
                                "value": not dev_dict["on"] if "on" in dev_dict else "unavailable from API"},
                               {"key": "paused", "value": get_key_from_dict("paused", dev_dict)},
                               {"key": "scheduleModeType", "value": get_key_from_dict("scheduleModeType", dev_dict)},
                               {"key": "status", "value": get_key_from_dict("status", dev_dict)},
                               {"key": "timeZone", "value": get_key_from_dict("timeZone", dev_dict)},
                               {"key": "utcOffset", "value": get_key_from_dict("utcOffset", dev_dict)}]
                # Get the current schedule for the device - it will tell us if it's running or not
                activeScheduleName = None
                try:
                    current_schedule_dict = self._make_api_call(
                        DEVICE_CURRENT_SCHEDULE_URL.format(apiVersion=RACHIO_API_VERSION, deviceId=dev_dict["id"]))
                    if len(current_schedule_dict):
                        # Something is running, so we need to figure out if it's a manual or automatic schedule and
                        # if it's automatic (a Rachio schedule) then we need to get the name of that schedule
                        #
                        # 2026-08-17 (Ham + Claude): same write-race guard as
                        # _update_from_rachio -- this path only runs once, at
                        # initial device setup, so the race is unlikely here,
                        # but applying the same check costs nothing and keeps
                        # both zone-write sites consistent.
                        if self._shouldAcceptZoneUpdate(
                                dev.id, current_schedule_dict.get("zoneStartDate", 0)):
                            update_list.append(
                                {"key": "activeZone", "value": current_schedule_dict["zoneNumber"]})
                        if current_schedule_dict["type"] == "AUTOMATIC":
                            schedule_detail_dict = self._make_api_call(
                                SCHEDULERULE_URL.format(apiVersion=RACHIO_API_VERSION,
                                                        scheduleRuleId=current_schedule_dict["scheduleRuleId"]))
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
                except (Exception,):
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
                self.logger.error(f"Rachio device '{dev.name}' configured with unknown ID. Reconfigure the device to make it active.")

        # set up webhooks for this device

        if not self.use_webhooks:
            return

        url = (API_URL + "notification/webhook_event_type").format(apiVersion=RACHIO_API_VERSION)
        reply = self._make_api_call(url)
        for r in reply:
            url = (API_URL + "notification/webhook").format(apiVersion=RACHIO_API_VERSION)
            data = {
                "device": {"id": dev.pluginProps["id"]},
                "externalId": dev.id,
                "url": self.webhook_url,
                "eventTypes": [{"id": r[u'id']}]
            }
            try:
                self._make_api_call(url, request_method="post", data=data)
                self.logger.debug(f"subscribed to webhook: {r['name']} ({r['id']})")
            except (Exception,):
                self.logger.debug(f"subscription failure for webhook: {r['name']} ({r['id']})")

    ########################################
    def deviceStopComm(self, dev):

        # remove webhooks
        if not self.use_webhooks:
            return

        url = (API_URL + "notification/{devId}/webhook").format(apiVersion=RACHIO_API_VERSION, devId=dev.pluginProps["id"])
        reply = self._make_api_call(url)
        for r in reply:
            url = (API_URL + "notification/webhook/{whid}").format(apiVersion=RACHIO_API_VERSION, whid=r["id"])
            try:
                self._make_api_call(url, request_method="delete")
                self.logger.debug(f"unsubscribed from webhook: {r['eventTypes'][0]['name']} ({r['id']})")
            except (Exception,):
                self.logger.debug(f"unsubscribe failure for webhook: {r['eventTypes'][0]['name']} ({r['id']})")

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
                        trigger_type = trigger.pluginProps["errorType"]
                        if trigger_type == "all" and event in ALL_OPERATIONAL_ERROR_EVENTS:
                            indigo.trigger.execute(trigger)
                        # then we fire if the event specifically matches the trigger type
                        if trigger_type == event:
                            indigo.trigger.execute(trigger)
                elif trigger.pluginTypeId == "commError":
                    trigger_type = trigger.pluginProps["errorType"]
                    # first we fire the trigger if it's any comm error in the ALL_COMM_ERROR_EVENTS list
                    if trigger_type == "allCommErrors" and event in ALL_COMM_ERROR_EVENTS:
                        indigo.trigger.execute(trigger)
                    # then we fire if the event specifically matches the trigger type
                    if trigger_type == event:
                        indigo.trigger.execute(trigger)
                elif trigger.pluginTypeId == event:
                    # an update is available, just fire the trigger since there's nothing else to look at
                    indigo.trigger.execute(trigger)
        except Exception as exc:
            self.logger.error(u"An error occurred during trigger processing")
            self.logger.debug(f"An error occurred during trigger processing: \n{traceback.format_exc(10)}")

    ########################################
    def triggerStartProcessing(self, trigger):
        super(Plugin, self).triggerStartProcessing(trigger)
        self.logger.debug(f"Start processing trigger {str(trigger.id)}")
        if trigger.id not in self.triggerDict:
            self.triggerDict[trigger.id] = trigger
        self.logger.debug(f"Start trigger processing list: {str(self.triggerDict)}")

    ########################################
    def triggerStopProcessing(self, trigger):
        super(Plugin, self).triggerStopProcessing(trigger)
        self.logger.debug("Stop processing trigger " + str(trigger.id))
        try:
            del self.triggerDict[trigger.id]
        except (Exception,):
            # the trigger isn't in the list for some reason so just skip it
            pass
        self.logger.debug(f"Stop trigger processing list: {str(self.triggerDict)}")

    ########################################
    # Sprinkler Control Action callback
    ########################################
    def actionControlSprinkler(self, action, dev):
        # ZONE ON #
        if action.sprinklerAction == indigo.kSprinklerAction.ZoneOn:
            if self.throttle_next_call:
                self.logger.error(f"API calls have violated rate limit - next connection attempt at {self.throttle_next_call:%H:%M:%S}"
                                  )
                self._fireTrigger("startZoneFailed", dev.id)
            else:
                zone_dict = self._get_zone_dict(dev.states["id"], action.zoneIndex)
                self.logger.debug(f"zone_dict: {zone_dict}")
                if zone_dict:
                    zoneName = zone_dict["name"]
                    data = {
                        "id": zone_dict["id"],
                        "duration": zone_dict["maxRuntime"] if zone_dict["maxRuntime"] <= self.maxZoneRunTime else self.maxZoneRunTime,
                    }
                    try:
                        self._make_api_call(ZONE_START_URL.format(apiVersion=RACHIO_API_VERSION), request_method="put",
                                            data=data)
                        self.logger.info(f'sent "{dev.name} - {zoneName}" on')
                        dev.updateStateOnServer("activeZone", action.zoneIndex)
                    except (Exception,):
                        # Else log failure but do NOT update state on Indigo Server. Also, fire any triggers the user has
                        # on zone start failures.
                        self.logger.error(f'send "{dev.name} - {zoneName}" on failed')
                        self.logger.debug(f"API error: \n{traceback.format_exc(10)}")
                        self._fireTrigger("startZoneFailed", dev.id)
                else:
                    self.logger.error(f"Zone number {action.zoneIndex} doesn't exist in this controller and can't be enabled.")
                    self._fireTrigger("startZoneFailed", dev.id)

        # ALL ZONES OFF #
        elif action.sprinklerAction == indigo.kSprinklerAction.AllZonesOff:
            data = {
                "id": dev.states["id"],
            }
            try:
                self._make_api_call(DEVICE_STOP_WATERING_URL.format(apiVersion=RACHIO_API_VERSION), request_method="put", data=data)
                self.logger.info(f'sent "{dev.name}" {"all zones off"}')
                dev.updateStateOnServer("activeZone", 0)
            except (Exception,):
                # Else log failure but do NOT update state on Indigo Server.
                self.logger.info(f'send "{dev.name}" {"all zones off"} failed')
                self.logger.debug(f"API error: \n{traceback.format_exc(10)}")
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
    def _refresh_device_schedule(self, dev):
        """2026-09-01 (Ham + Claude): lean status-request refresh. Fetches ONLY the
        given controller's current_schedule and updates its active-zone / schedule
        states — skipping the account/person call, the other controllers, the
        weather forecast, and the per-cycle zone-property rewrite. Mirrors the
        current-schedule block in _update_from_rachio and reuses the same
        _shouldAcceptZoneUpdate race guard, so zone-tracking accuracy is identical;
        it just costs one API call instead of a full-account refresh. Used when
        PluginConfig 'statusRequestScope' == 'controller'."""
        if not self.access_token:
            return
        try:
            device_uuid = dev.states["id"]
        except Exception:
            return
        update_list = []
        try:
            current_schedule_dict = self._make_api_call(
                DEVICE_CURRENT_SCHEDULE_URL.format(apiVersion=RACHIO_API_VERSION, deviceId=device_uuid))
            if len(current_schedule_dict):
                # Same stale-write guard as the full poll (skip only the zone keys
                # if this response is for an already-superseded zone).
                if self._shouldAcceptZoneUpdate(dev.id, current_schedule_dict.get("zoneStartDate", 0)):
                    update_list.append({"key": "activeZone", "value": current_schedule_dict["zoneNumber"]})
                    update_list.append({"key": "activeZoneDuration",
                                        "value": current_schedule_dict.get("zoneDuration", 0)})
                    update_list.append({"key": "activeZoneStartDate",
                                        "value": current_schedule_dict.get("zoneStartDate", 0) // 1000})
                else:
                    self.logger.debug(f"{dev.name}: skipping stale activeZone write (lean refresh).")
                update_list.append({"key": "activeScheduleDuration",
                                    "value": current_schedule_dict.get("duration", 0)})
                update_list.append({"key": "activeScheduleStartDate",
                                    "value": current_schedule_dict.get("startDate", 0) // 1000})
                if current_schedule_dict["type"] == "AUTOMATIC":
                    schedule_detail_dict = self._make_api_call(
                        SCHEDULERULE_URL.format(apiVersion=RACHIO_API_VERSION,
                                                scheduleRuleId=current_schedule_dict["scheduleRuleId"]))
                    update_list.append({"key": "activeSchedule", "value": schedule_detail_dict["name"]})
                else:
                    update_list.append({"key": "activeSchedule",
                                        "value": current_schedule_dict["type"].title()})
            else:
                # No active schedule: mirror the full poll's zone-off writes. Leave
                # activeScheduleDuration/StartDate and activeSchedulePlan untouched
                # (the full poll deliberately keeps them readable after a run ends).
                update_list.append({"key": "activeSchedule", "value": "No active schedule"})
                update_list.append({"key": "activeZone", "value": 0})
                update_list.append({"key": "activeZoneDuration", "value": 0})
                update_list.append({"key": "activeZoneStartDate", "value": 0})
        except Exception:
            update_list.append({"key": "activeSchedule", "value": "Error getting current schedule"})
            self.logger.debug("API error (lean refresh):\n{}".format(traceback.format_exc(10)))
            self._fireTrigger("getScheduleCall")
        if len(update_list):
            dev.updateStatesOnServer(update_list)

    def actionControlUniversal(self, action, dev):
        # STATUS REQUEST #
        if action.deviceAction == indigo.kUniversalAction.RequestStatus:
            # 2026-09-01 (Ham + Claude): status-request scope is user-selectable
            # (PluginConfig "statusRequestScope"). "controller" = refresh ONLY the
            # requested controller's current schedule/active zone (one API call),
            # skipping the account/person call, other controllers, and weather —
            # so a flow monitor polling status every 30s during a run doesn't do a
            # full-account refresh (+ forced weather) each time. "all" (default) is
            # the original behavior. A global request (dev is None) always does full.
            if self.pluginPrefs.get("statusRequestScope", "all") == "controller" and dev is not None:
                self._refresh_device_schedule(dev)
            else:
                # 2026-09-01 (Ham + Claude): E — do NOT force a weather refresh on an
                # "all"-mode status request. This previously reset _next_weather_update
                # to now, so every RequestStatus pulled a fresh 14-day forecast per
                # controller. Weather now rides its own DEFAULT_WEATHER_UPDATE_INTERVAL
                # timer (post-A) inside _update_forecast_data; status requests refresh
                # device/schedule state only.
                self._update_from_rachio()

    ########################################
    # Custom Plugin Action callbacks defined in Actions.xml
    ########################################
    def runRachioSchedule(self, pluginAction, dev):
        schedule_rule_id = pluginAction.props["scheduleId"]
        if self.throttle_next_call:
            self.logger.error(f"API calls have violated rate limit - next connection attempt at {self.throttle_next_call:%H:%M:%S}"
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
                        self.logger.info(f"Rachio schedule '{schedule_id_dict[schedule_rule_id]}' started")
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
        except (Exception,):
            self.logger.error("Seasonal adjustments must be specified as an integer from -100 to 100 (a percentage)")
            return
        if self.throttle_next_call:
            self.logger.error(f"API calls have violated rate limit - next connection attempt at {self.throttle_next_call:%H:%M:%S}"
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
                        self.logger.info(
                            f"Rachio seasonal adjustment set to {pluginAction.props['adjustment']}%")
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
            self.logger.info(f"Standby mode for controller '{dev.name}' turned {'on' if pluginAction.props['mode'] else 'off'}")
        except Exception as exc:
            self.logger.error("Could not set standby mode - check your controller.")
            self.logger.debug(f"API error: \n{traceback.format_exc(10)}")
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

    def toggleStandbyMode(self, valuesDict, typeId):
        try:
            deviceId = int(valuesDict["targetDevice"])
            dev = indigo.devices[deviceId]
        except (Exception,):
            self.logger.error(u"Bad Device specified for Toggle Standby Mode operation")
            return False

        try:
            data = {
                "id": dev.states["id"],
            }
            if dev.onState:
                url = DEVICE_TURN_OFF_URL.format(apiVersion=RACHIO_API_VERSION)
            else:
                url = DEVICE_TURN_ON_URL.format(apiVersion=RACHIO_API_VERSION)

            self._make_api_call(url, request_method="put", data=data)
            self.logger.info("{}: Toggling standby mode".format(dev.name))
        except Exception as exc:
            self.logger.error("Could not set standby mode - check your controller.")
            self.logger.debug(f"API error: \n{traceback.format_exc(10)}")
            self._fireTrigger("setStandbyFailed", dev.id)

    ########################################
    def updateAllStatus(self):
        self._next_weather_update = datetime.now()
        self._update_from_rachio()

    ########################################
    def pickController(self, dev_filter=None, valuesDict=None, typeId=0):
        self.logger.threaddebug(f"pickController")
        retList = []
        for dev in indigo.devices.iter("self"):
            retList.append((dev.id, dev.name))
        retList.sort(key=lambda tup: tup[1])
        return retList

    # doesn't do anything, just needed to force other menus to dynamically refresh
    def configMenuChanged(self, valuesDict):
        self.logger.threaddebug(f"configMenuChanged")
        return valuesDict
