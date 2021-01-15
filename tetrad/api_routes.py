from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import request, jsonify
import functools
from google.cloud.bigquery import Client, QueryJobConfig, ScalarQueryParameter
from tetrad import app, cache, admin_utils, gaussian_model_utils, limiter, utils
import json
import numpy as np 
from os import getenv
import pandas as pd 
import re 
import requests
from time import time 
import google.cloud.logging 
gcloud_logging_client = google.cloud.logging.Client()
gcloud_logging_client.get_default_handler()
gcloud_logging_client.setup_logging()
import logging
logging.error("Inside api_routes.py")


# Get env variables
PROJECT_ID = getenv("GOOGLE_CLOUD_PROJECT")
BIGQUERY_TABLE_SLC = getenv("BIGQUERY_TABLE_SLC")
SPACE_KERNEL_FACTOR_PADDING = float(getenv("SPACE_KERNEL_FACTOR_PADDING"))
TIME_KERNEL_FACTOR_PADDING = float(getenv("TIME_KERNEL_FACTOR_PADDING"))

bq_client = Client(project=PROJECT_ID)
elevation_interpolator = utils.setupElevationInterpolator()

SRC_MAP = {
    "SLC": BIGQUERY_TABLE_SLC,
}

# VALID_SENSOR_SOURCES = ["AirU", "all"]

# keys = local fields, values = table fields
TBL_MAP = {
    "Elevation": "Elevation",
    "MICS_RED": "MICS_RED",
    "Humidity": "Humidity",
    "DeviceID": "DeviceID",
    "Latitude": "Latitude",
    "Longitude": "Longitude",
    "MICS_Heater": "MICS_HEATER",
    "MICS_OX": "MICS_OX",
    "PM1": "PM1",
    "PM10": "PM10",
    "PM2_5": "PM2_5",
    # "POSIX": "POSIX",
    # "Uptime": "SecActive",
    # "VersionInfo": "SensorModel",
    "Temperature": "Temperature",
    # "topic": "topic",
    "Timestamp": "Timestamp",
}

VALID_QUERY_FIELDS = dict((k, TBL_MAP[k]) for k in 
    [
        "PM1",
        "PM2_5",
        "PM10",
        "Temperature",
        "Humidity",
        "MICS_RED",
        "MICS_OX",
        "MICS_Heater",
        # "Uptime",
        # "VersionInfo",
    ])


@app.route("/liveSensors", methods=["GET"], subdomain=getenv('SUBDOMAIN_API'))
# @cache.cached(timeout=1800)
def liveSensors():

    logging.error(request.view_args)
    logging.error(request.args)
    logging.error(vars(request.args))
    req_args = [
        'src'
        ]

    r = utils.checkArgs(request.args, req_args)
    if r[1] != 200: 
        return r

    src = request.args.get('src')
    if src not in SRC_MAP:
        return jsonify({'Error': f"argument 'src' must be included from {', '.join(list(SRC_MAP.keys()))}"}), 400
    src_tbl = SRC_MAP[request.args.get('src').upper()]

    # Optional argument: 'delta' - how far in the past (minutes) to look
    if 'delta' in request.args:
        delta = request.args.get('delta', type=int)
    else:
        delta = 24 * 60
    assert (delta > 0), "'delta' must be a positive integer (minutes)"

    q = f"""SELECT 
                {TBL_MAP["DeviceID"]}  AS DeviceID, 
                {TBL_MAP["Timestamp"]} AS Timestamp, 
                {TBL_MAP["Latitude"]}  AS Latitude, 
                {TBL_MAP["Longitude"]} AS Longitude, 
                {TBL_MAP["PM2_5"]}     AS PM2_5
            FROM
                (
                    SELECT 
                        *, 
                        ROW_NUMBER() 
                            OVER (
                                    PARTITION BY 
                                        {TBL_MAP["DeviceID"]} 
                                    ORDER BY 
                                        {TBL_MAP["Timestamp"]} DESC
                            ) row_num
                    FROM 
                        `{src_tbl}`
                    WHERE 
                        {TBL_MAP["Timestamp"]} >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {delta} MINUTE)
                )
            WHERE 
                row_num = 1;
    """

    query_job = bq_client.query(q)
    rows = query_job.result()
    
    data = []
    for row in rows:
        data.append(
            {
                "DeviceID": str(row.DeviceID),
                "Latitude": row.Latitude,
                "Longitude": row.Longitude,
                "Timestamp": row.Timestamp,
                "PM2_5": row.PM2_5
            })

    # Apply correction factors 
    data = utils.applyCorrectionFactorsToList(data)

    return jsonify(data), 200

# ***********************************************************
# Function: request_data_flask(d) - used to populate the map with sensors
# Called by slider.js
# Parameter:
# Return: Last recorded sensor input from all sensors in the DB
# ***********************************************************
@app.route("/request_data_flask", methods=['GET'])
def request_data_flask(d):
    sensor_list = []
 
    logging.error(request.view_args)
    logging.error(request.args)
    logging.error(vars(request.args))
    req_args = [
        'src'
        ]

    r = utils.checkArgs(request.args, req_args)
    if r[1] != 200: 
        return r

    src = request.args.get('src')
    if src not in SRC_MAP:
        return jsonify({'Error': f"argument 'src' must be included from {', '.join(list(SRC_MAP.keys()))}"}), 400
    sensors_table = SRC_MAP[request.args.get('src').upper()]

    # get the latest sensor data from each sensor
    q = ("SELECT `" + sensor_table + "`.DEVICEID, PM2_5, Latitude, Longitude, Timestamp "
         "FROM `" + sensor_table + "` "
         "INNER JOIN (SELECT DEVICEID, MAX(Timestamp) as maxts "
         "FROM `" + sensor_table + "` GROUP BY DEVICEID) mr "
         "ON `" + sensor_table + "`.DEVICEID = mr.DEVICEID AND Timestamp = maxts WHERE Timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 10 DAY);")

    query_job = bq_client.query(q)
    rows = query_job.result()  # Waits for query to finish
    for row in rows:
        sensor_list.append({"DEVICEID": str(row.DEVICEID),
                            "Latitude": row.Latitude,
                            "Longitude": row.Longitude,
			    "Timestamp": row.Timestamp,
                            "PM2_5": row.PM2_5})

    

    return jsonify(sensor_list), 200



# ***********************************************************
# Function: request_historical(d)
# Called by historical_data.js
# Parameter: JSON format containing DEVICE_ID, DAYS (number of days to query ), and CITY ( to be discussed )
# Return: JSON sensor data for the given sensor and given number of days
# ***********************************************************
@app.route("/request_historical/<d>/", methods=['GET'])
def request_historical(d):
    sensor_list = []
    json_data = json.loads(d)
    id = json_data["DEVICEID"]
    days = str(json_data["DAYS"])
    sensor_table = "tetrad-296715.telemetry." + json_data["CITY"]
    # get the latest sensor data from each sensor
    q = ("SELECT DATETIME_TRUNC(DATETIME(Timestamp, 'America/Denver'), HOUR) AS D, ROUND(AVG(PM2_5),2) AS PM2_5 "
        "FROM `" + sensor_table + "` "
        "WHERE DEVICEID=\'" + id + "\' "
        "AND Timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL " + days + " DAY) "
        "GROUP BY D ORDER BY D;")

    query_job = bq_client.query(q)
    rows = query_job.result()  # Waits for query to finish
    for row in rows:
        sensor_list.append({"D_TIME": row.D,
                            "PM2_5": row.PM2_5})

    return jsonify(sensor_list), 200


# @app.route("/api/timeAggregatedDataFrom", methods=["GET"])
# def timeAggregatedDataFrom():
#     # Get the arguments from the query string
#     id = request.args.get('id')
#     sensor_source = request.args.get('sensorSource')
#     start = request.args.get('start')
#     end = request.args.get('end')
#     function = request.args.get('function')
#     timeInterval = request.args.get('timeInterval')  # Time interval in minutes

#     SQL_FUNCTIONS = {
#         "mean": "AVG",
#         "min": "MIN",
#         "max": "MAX",
#     }

#     # Check ID is valid
#     if id == "" or id == "undefined":
#         msg = "id is invalid. It must be a string that is not '' or 'undefined'."
#         return msg, 400

#     # Check that sensor_source is valid
#     if sensor_source not in VALID_SENSOR_SOURCES:
#         msg = f"sensor_source is invalid. It must be one of {VALID_SENSOR_SOURCES}"
#         return msg, 400

#     # Check aggregation function is valid
#     if function not in SQL_FUNCTIONS:
#         msg = f"function is not in {SQL_FUNCTIONS.keys()}"
#         return msg, 400

#     # Check that the data is formatted correctly
#     if not utils.validateDate(start) or not utils.validateDate(end):
#         msg = "Incorrect date format, should be {utils.DATETIME_FORMAT}, e.g.: 2018-01-03T20:00:00Z"
#         return msg, 400

#     # Define the BigQuery query
#     tables_list = []
#     if sensor_source == "AirU" or sensor_source == "all":
#         tables_list.append(
#             f"""(
#                 SELECT ID, time, PM2_5, Latitude, Longitude, SensorModel, 'AirU' as SensorSource
#                 FROM `{AIRU_TABLE_ID}`
#                 WHERE time >= @start
#             )"""
#         )

#     if sensor_source == "PurpleAir" or sensor_source == "all":
#         tables_list.append(
#             f"""(
#                 SELECT ID, time, PM2_5, Latitude, Longitude, '' as SensorModel, 'PurpleAir' as SensorSource
#                 FROM `{PURPLEAIR_TABLE_ID}`
#                 WHERE time >= @start
#             )"""
#         )

#     if sensor_source == "DAQ" or sensor_source == "all":
#         tables_list.append(
#             f"""(
#                 SELECT ID, time, PM2_5, Latitude, Longitude, '' as SensorModel, 'DAQ' as SensorSource
#                 FROM `{DAQ_TABLE_ID}`
#                 WHERE time >= @start
#             )"""
#         )

#     query = f"""
#         WITH
#             intervals AS (
#                 SELECT
#                     TIMESTAMP_ADD(@start, INTERVAL @interval * num MINUTE) AS lower,
#                     TIMESTAMP_ADD(@start, INTERVAL @interval * 60* (1 + num) - 1 SECOND) AS upper
#                 FROM UNNEST(GENERATE_ARRAY(0,  DIV(TIMESTAMP_DIFF(@end, @start, MINUTE) , @interval))) AS num
#             )
#         SELECT
#             CASE WHEN {SQL_FUNCTIONS.get(function)}(PM2_5) IS NOT NULL
#                 THEN {SQL_FUNCTIONS.get(function)}(PM2_5)
#                 ELSE 0
#                 END AS PM2_5,
#             upper
#         FROM intervals
#             JOIN (
#             {' UNION ALL '.join(tables_list)}
#         ) sensors
#             ON sensors.time BETWEEN intervals.lower AND intervals.upper
#         WHERE ID = @id
#         GROUP BY upper
#         ORDER BY upper
#     """

#     job_config = QueryJobConfig(
#         query_parameters=[
#             ScalarQueryParameter("id", "STRING", id),
#             ScalarQueryParameter("start", "TIMESTAMP", start),
#             ScalarQueryParameter("end", "TIMESTAMP", end),
#             ScalarQueryParameter("interval", "INT64", timeInterval),
#         ]
#     )

#     # Run the query and collect the result
#     measurements = []
#     query_job = bq_client.query(query, job_config=job_config)
#     rows = query_job.result()
#     for row in rows:
#         measurements.append({"PM2_5": row.PM2_5, "time": row.upper.strftime(utils.DATETIME_FORMAT)})

#     tags = [{
#         "ID": id,
#         "SensorSource": sensor_source,
#         "SensorModel": "H1.2+S1.0.8",
#         "time": datetime.utcnow().strftime(utils.DATETIME_FORMAT)
#     }]
#     return jsonify({"data": measurements, "tags": tags})


@app.route("/requestField", methods=["GET"], subdomain=getenv('SUBDOMAIN_API'))
def requestField():
    """
    Arguments:
    @param: qtype   (required)  
    @param: field   (required)
    @param: start   (required)
    @param: end     (required)
    
    Radius:
    @param: lat
    @param: lon
    @param: radius

    Bounding Box:
    @param: latN
    @param: latS
    @param: lonE
    @param: lonW

    Possible queries:
    - [1] Get field from all DeviceIDs
    - [2] Get field from select DeviceID[s]
    - [3] Get field in radius from all DeviceIDs
    - [4] Get field in radius from select DeviceID[s]
    - [5] Get field in bbox from all DeviceIDs
    - [6] Get field in bbox from select DeviceIDs
    """
    
    req_args = [
        'src', 
        'field', 
        'start', 
        'end',
        ]

    r = utils.checkArgs(request.args, req_args)
    if r[1] != 200: 
        return r

    src   = request.args.get('src', type=str)
    field = request.args.get('field', type=str)
    start = request.args.get('start', type=str)
    end   = request.args.get('end', type=str)
    
    # Check source is valid
    if src not in SRC_MAP:
        return jsonify({'Error': f"argument 'src' must be included from {', '.join(list(SRC_MAP.keys()))}"}), 400
    src_tbl = SRC_MAP[request.args.get('src').upper()]

    # Check field is valid
    if field not in VALID_QUERY_FIELDS:
        msg = "Parameter <code>field</code> is invalid. Must be one of the following:<ul>"
        msg += ''.join([f"<li><code>{f}</code></li>" for f in VALID_QUERY_FIELDS])
        msg += '</ul><br>Furthermore, only a single field at a time is supported.'
        return msg, 400

    # Check that datetimes are valid
    if not utils.validateDate(start) or not utils.validateDate(end):
        msg = f"Incorrect date format, should be {utils.DATETIME_FORMAT}, e.g.: 2018-01-03T20:00:00Z"
        return msg, 400

    if 'devices' in request.args:
        devices = request.args.getlist('devices')
        # TODO: Test to see what happens with one list item
    else: 
        devices = None

    # Perform query and post-query cleanup
    response = _requestField(src_tbl, field, start, end, id_ls=devices)
    if response[1] != 200:
        return response 
    else:
        data = response[0] 

    return data, 200
    


def _requestField(src_tbl, field, start, end, id_ls=None):
    """Function to query a field (like Temperature, Humidity, PM, etc.) 
    in date range [start, end]. Can include an ID or a list of IDs"""

    if id_ls:
        idstr = utils.idsToWHEREClause(id_ls, TBL_MAP['DeviceID'])
    else:
        idstr = "True"
    print("idstr:", idstr)

    # Build the query
    QUERY = f"""
        SELECT
            {TBL_MAP['Timestamp']} AS Timestamp,
            {TBL_MAP['DeviceID']}  AS DeviceID,
            {TBL_MAP['Latitude']}  AS Latitude,
            {TBL_MAP['Longitude']} AS Longitude,
            {TBL_MAP['Elevation']} AS Elevation,
            {field}
        FROM 
            `{src_tbl}`
        WHERE
            {TBL_MAP['Timestamp']} >= @start
            AND 
            {TBL_MAP['Timestamp']} <= @end
            AND
            {idstr}
        ORDER BY 
            Timestamp;        
    """

    job_config = QueryJobConfig(
        query_parameters=[
            ScalarQueryParameter("start", "TIMESTAMP", start),
            ScalarQueryParameter("end", "TIMESTAMP", end),
        ]
    )

    print(QUERY)

    # Run the query and collect the result
    query_job = bq_client.query(QUERY, job_config=job_config)

    if query_job.error_result:
        print(query_job.error_result)

        # TODO: Is it okay to dump this info? 
        return f"Invalid API call - check documentation.<br>{query_job.error_result}", 400

    # Wait for query to finish
    rows = query_job.result()

    # Format as proper dicts
    data = []
    
    # break on empty iterator
    if rows.total_rows == 0:
        msg = "No data returned"
        return msg, 200

    for row in rows:
        data.append({
            "DeviceID": str(row.DeviceID),
            "Latitude": row.Latitude,
            "Longitude": row.Longitude,
            "Timestamp": row.Timestamp,
            f"{field}": row[field]
        })

    # Apply correction factors to data
    return jsonify(utils.applyCorrectionFactorsToList(data)), 200


@app.route("/requestFieldInRadius", methods=["GET"], subdomain=getenv('SUBDOMAIN_API'))
def requestFieldInRadius():

    req_args = [
        'src', 
        'field', 
        'lat', 
        'lon', 
        'radius', 
        'start', 
        'end'
    ]

    r = utils.checkArgs(request.args, req_args)
    if r[1] != 200: 
        return r

    src = request.args.get('src', type=str)
    field = request.args.get('field', type=str)
    lat = request.args.get('lat', type=float)
    lon = request.args.get('lon', type=float)
    radius = request.args.get('radius', type=float)
    start = request.args.get('start', type=str)
    end = request.args.get('end', type=str)

    # Check source is valid
    if src not in SRC_MAP:
        return jsonify({'Error': f"argument 'src' must be included from {', '.join(list(SRC_MAP.keys()))}"}), 400
    src_tbl = SRC_MAP[request.args.get('src').upper()]

    # Check field is valid
    if field not in VALID_QUERY_FIELDS:
        msg = "Parameter <code>field</code> is invalid. Must be one of the following:<ul>"
        msg += ''.join([f"<li><code>{f}</code></li>" for f in VALID_QUERY_FIELDS])
        msg += '</ul><br>Furthermore, only a single field at a time is supported.'
        return msg, 400
    
    # Check lat/lon valid
    if not utils.validLatLon(lat, lon):
        msg = "Parameter <code>lat</code> or <code>lon</code> is invalid. [-90 < lat < 90], [-180 < lon < 180]"
        return msg, 400

    # Check radius valid
    if not utils.validRadius(radius):
        msg = "Parameter <code>radius</code> must be a positive float, in meters."
        return msg, 400

    # Check that datetimes are valid
    if not utils.validateDate(start) or not utils.validateDate(end):
        msg = f"Incorrect date format, should be {utils.DATETIME_FORMAT}, e.g.: 2018-01-03T20:00:00Z"
        return msg, 400

    # Perform query and post-query cleanup
    response = _requestFieldInRadius(src_tbl, field, lat, lon, radius, start, end)
    if response[1] != 200:
        return response 
    else:
        data = response[0] 

    return data, 200


def _requestFieldInRadius(src_tbl, field, lat, lon, radius, start, end):
    """Function to query a field (like Temperature, Humidity, PM, etc.) 
    in a radius (kilometers) around GPS (lat,lon) in date range [start, end]"""

    # Build the query
    QUERY = f"""
        CREATE TEMP FUNCTION toRad(x FLOAT64) AS ((x * ACOS(-1)) / 180);
        SELECT
            {TBL_MAP['Timestamp']} AS Timestamp,
            {TBL_MAP['DeviceID']} AS DeviceID,
            {TBL_MAP['Latitude']} AS Latitude,
            {TBL_MAP['Longitude']} AS Longitude,
            {TBL_MAP['Elevation']} AS Elevation,
            {field}
        FROM 
            `{src_tbl}`
        WHERE
            Timestamp >= @start
            AND 
            Timestamp <= @end
            AND
            ACOS(
                SIN( toRad(@lat) ) * 
                SIN( toRad(`Latitude`) ) + 
                COS( toRad(@lat) ) * 
                COS( toRad(`Latitude`) ) * 
                COS( toRad(`Longitude`) - toRad(@lon) )
            ) * 6371 <= @radius
        ORDER BY 
            `Timestamp`;        
    """

    job_config = QueryJobConfig(
        query_parameters=[
            ScalarQueryParameter("lat", "NUMERIC", lat),
            ScalarQueryParameter("lon", "NUMERIC", lon),
            ScalarQueryParameter("radius", "NUMERIC", radius),
            ScalarQueryParameter("start", "TIMESTAMP", start),
            ScalarQueryParameter("end", "TIMESTAMP", end),
        ]
    )

    print(QUERY)

    # Run the query and collect the result
    query_job = bq_client.query(QUERY, job_config=job_config)

    if query_job.error_result:
        print(query_job.error_result)

        # TODO: Is it okay to dump this info? 
        return f"Invalid API call - check documentation.<br>{query_job.error_result}", 400

    # Wait for query to finish
    rows = query_job.result()

    # Format as proper dicts
    model_data = []
    
    # break on empty iterator
    if rows.total_rows == 0:
        msg = "No data returned"
        return msg, 200

    for row in rows:
        model_data.append({
            "DeviceID": str(row.DeviceID),
            "Latitude": row.Latitude,
            "Longitude": row.Longitude,
            "Timestamp": row.Timestamp,
            f"{field}": row[field]
        })

    # Apply correction factors to data
    return jsonify(utils.applyCorrectionFactorsToList(model_data)), 200


# # Gets data within radius of the provided lat lon within the time frame. The radius units are latlon degrees so this is an approximate bounding circle
# def request_model_data_local(lat, lon, radius, start_date, end_date):
#     model_data = []
#     # get the latest sensor data from each sensor
#     query = f"""
#         SELECT 
#             ID, time, PM2_5, Latitude, Longitude
#         FROM 
#             `{BIGQUERY_TABLE_SLC}`
#         WHERE 
#             time > @start_date AND time < @end_date
#             AND
#             SQRT(POW(Latitude - @lat, 2) + POW(Longitude - @lon, 2)) <= @radius
#         ORDER BY time ASC
#     """

#     job_config = QueryJobConfig(
#         query_parameters=[
#             ScalarQueryParameter("lat", "NUMERIC", lat),
#             ScalarQueryParameter("lon", "NUMERIC", lon),
#             ScalarQueryParameter("radius", "NUMERIC", radius),
#             ScalarQueryParameter("start_date", "TIMESTAMP", utils.datetimeToBigQueryTimestamp(start_date)),
#             ScalarQueryParameter("end_date", "TIMESTAMP", utils.datetimeToBigQueryTimestamp(end_date)),
#         ]
#     )

#     query_job = bq_client.query(query, job_config=job_config)

#     if query_job.error_result:
#         print(query_job.error_result)
#         return "Invalid API call - check documentation.", 400
#     rows = query_job.result()  # Waits for query to finish

#     for row in rows:
#         model_data.append({
#             "ID": str(row.ID),
#             "Latitude": row.Latitude,
#             "Longitude": row.Longitude,
#             "time": row.time,
#             "PM2_5": row.PM2_5,
#             "SensorModel": row.SensorModel,
#             "SensorSource": row.SensorSource,
#         })

#     # Apply correction factors
#     model_data = utils.applyCorrectionFactorsToList(model_data)
#     # correction_factors = utils.loadCorrectionFactors(getenv("CORRECTION_FACTORS_FILENAME"))
#     # print(f'Loaded {len(correction_factors)} correction factors.')

#     # for datum in sensor_data:
#     #     datum['PM2_5'] = utils.applyCorrectionFactor(correction_factors, datum['time'], datum['PM2_5'], datum['type'])

#     return model_data


# Gets data within radius of the provided lat lon within the time frame. The radius units are latlon degrees so this is an approximate bounding circle
# @app.route("/api/request_model_data/", methods=['GET'])
# def request_model_data():
#     query_parameters = request.args
#     lat = query_parameters.get('lat')
#     lon = query_parameters.get('lon')
#     radius = query_parameters.get('radius')
#     query_start_date = request.args.get('start_date')
#     query_end_date = request.args.get('end_date')
#     if not utils.validateDate(query_start_date) or not utils.validateDate(query_end_date):
#         resp = jsonify({'message': f"Incorrect date format, should be {utils.DATETIME_FORMAT}, e.g.: 2018-01-03T20:00:00Z"})
#         return resp, 400

#     query_start_datetime = utils.parseDateString(query_start_date)
#     query_end_datetime = utils.parseDateString(query_end_date)
#     model_data = request_model_data_local(lat, lon, radius, query_start_datetime, query_end_datetime)
#     return jsonify(model_data)


# @app.route("/api/getPredictionsForLocation/", methods=['GET'])
# def getPredictionsForLocation():
#     # Check that the arguments we want exist
#     # if not validateInputs(['lat', 'lon', 'predictionsperhour', 'start_date', 'end_date'], request.args):
#     #     return 'Query string is missing one or more of lat, lon, predictionsperhour, start_date, end_date', 400

#     # step -1, parse query parameters
#     try:
#         query_lat = float(request.args.get('lat'))
#         query_lon = float(request.args.get('lon'))
#         query_period = float(request.args.get('predictionsperhour'))
#     except ValueError:
#         return 'lat, lon, predictionsperhour must be floats.', 400

#     query_start_date = request.args.get('start_date')
#     query_end_date = request.args.get('end_date')

#     # Check that the data is formatted correctly
#     if not utils.validateDate(query_start_date) or not utils.validateDate(query_end_date):
#         msg = f"Incorrect date format, should be {utils.DATETIME_FORMAT}, e.g.: 2018-01-03T20:00:00Z"
#         return msg, 400

#     query_start_datetime = utils.parseDateString(query_start_date)
#     query_end_datetime = utils.parseDateString(query_end_date)

#     print((
#         f"Query parameters: lat={query_lat} lon={query_lon} start_date={query_start_datetime}"
#         f" end_date={query_end_datetime} predictionsperhour={query_period}"
#     ))

#     # step 0, load up the bounding box from file and check that request is within it
#     bounding_box_vertices = utils.loadBoundingBox()
#     print(f'Loaded {len(bounding_box_vertices)} bounding box vertices.')

#     if not utils.isQueryInBoundingBox(bounding_box_vertices, query_lat, query_lon):
#         return 'The query location is outside of the bounding box.', 400

#     # step 1, load up correction factors from file
#     correction_factors = utils.loadCorrectionFactors()
#     print(f'Loaded {len(correction_factors)} correction factors.')

#     # step 2, load up length scales from file
#     length_scales = utils.loadLengthScales()
#     print(f'Loaded {len(length_scales)} length scales.')

#     print('Loaded length scales:', length_scales, '\n')
#     length_scales = utils.getScalesInTimeRange(length_scales, query_start_datetime, query_end_datetime)
#     if len(length_scales) < 1:
#         msg = (
#             f"Incorrect number of length scales({len(length_scales)}) "
#             f"found in between {query_start_datetime} and {query_end_datetime}"
#         )
#         return msg, 400

#     latlon_length_scale = length_scales[0]['latlon']
#     elevation_length_scale = length_scales[0]['elevation']
#     time_length_scale = length_scales[0]['time']

#     print(
#         f'Using length scales: latlon={latlon_length_scale} elevation={elevation_length_scale} time={time_length_scale}'
#     )

#     # step 3, query relevent data
#     APPROX_METERS_PER_LATLON_DEGREE_IN_UTAH = 70000
#     radius = latlon_length_scale / APPROX_METERS_PER_LATLON_DEGREE_IN_UTAH  # convert meters to latlon degrees for db query
#     sensor_data = request_model_data_local(
#         lat=query_lat,
#         lon=query_lon,
#         radius=radius,
#         start_date=query_start_datetime - timedelta(hours=time_length_scale),
#         end_date=query_end_datetime + timedelta(hours=time_length_scale))

#     unique_sensors = {datum['ID'] for datum in sensor_data}
#     print(f'Loaded {len(sensor_data)} data points for {len(unique_sensors)} unique devices from bgquery.')

#     # step 3.5, convert lat/lon to UTM coordinates
#     try:
#         utils.convertLatLonToUTM(sensor_data)
#     except ValueError as err:
#         return f'{str(err)}', 400

#     sensor_data = [datum for datum in sensor_data if datum['zone_num'] == 12]

#     unique_sensors = {datum['ID'] for datum in sensor_data}
#     print((
#         "After removing points with zone num != 12: "
#         f"{len(sensor_data)} data points for {len(unique_sensors)} unique devices."
#     ))

#     # Step 4, parse sensor type from the version
#     sensor_source_to_type = {'AirU': '3003', 'PurpleAir': '5003', 'DAQ': '5003'}
#     for datum in sensor_data:
#         datum['type'] = sensor_source_to_type[datum['SensorSource']]

#     print(f'Fields: {sensor_data[0].keys()}')

#     # step 4.5, Data Screening
#     print('Screening data')
#     sensor_data = utils.removeInvalidSensors(sensor_data)

#     # step 5, apply correction factors to the data
#     for datum in sensor_data:
#         datum['PM2_5'] = utils.applyCorrectionFactor(correction_factors, datum['time'], datum['PM2_5'], datum['type'])

#     # step 6, add elevation values to the data
#     for datum in sensor_data:
#         if 'Altitude' not in datum:
#             datum['Altitude'] = elevation_interpolator([datum['Latitude']], [datum['Longitude']])[0]

#     # step 7, Create Model
#     model, time_offset = gaussian_model_utils.createModel(
#         sensor_data, latlon_length_scale, elevation_length_scale, time_length_scale)

#     # step 8, get predictions from model
#     query_dates = utils.interpolateQueryDates(query_start_datetime, query_end_datetime, query_period)
#     query_elevation = elevation_interpolator([query_lat], [query_lon])[0]
#     predictions = gaussian_model_utils.predictUsingModel(
#         model, query_lat, query_lon, query_elevation, query_dates, time_offset)

#     return jsonify(predictions)

######################  Testing this
### this allows multi lats/lons to be specified.  
# @app.route("/api/getEstimatesForLocations/", methods=['GET'])
# def getEstimatesForLocations():
#     # Check that the arguments we want exist
#     # if not validateInputs(['lat', 'lon', 'estimatesperhour', 'start_date', 'end_date'], request.args):
#     #     return 'Query string is missing one or more of lat, lon, estimatesperhour, start_date, end_date', 400

#     # step -1, parse query parameters
#     try:
#         query_rate = float(request.args.get('estimaterate'))
#     except ValueError:
#         return 'Estimates must be floats.', 400

# ## regular expression for floats
#     regex = r'[+-]?[0-9]+\.[0-9]+'
#     query_lats = np.array(re.findall(regex,request.args.get('lat'))).astype(np.float)
#     query_lons = np.array(re.findall(regex,request.args.get('lon'))).astype(np.float)
#     if (query_lats.shape != query_lons.shape):
#         return 'lat, lon must be equal sized arrays of floats:'+str(query_lats)+' ; ' + str(query_lons), 400

#     num_locations = query_lats.shape[0]

#     query_start_date = request.args.get('start_date')
#     query_end_date = request.args.get('end_date')

#     # Check that the data is formatted correctly
#     if not utils.validateDate(query_start_date) or not utils.validateDate(query_end_date):
#         msg = f"Incorrect date format, should be {utils.DATETIME_FORMAT}, e.g.: 2018-01-03T20:00:00Z"
#         return msg, 400

#     query_start_datetime = utils.parseDateString(query_start_date)
#     query_end_datetime = utils.parseDateString(query_end_date)

#     print((
#         f"Query parameters: lat={query_lats} lon={query_lons} start_date={query_start_datetime}"
#         f" end_date={query_end_datetime} estimaterate={query_rate}"
#     ))

#     # step 0, load up the bounding box from file and check that request is within it
#     bounding_box_vertices = utils.loadBoundingBox()
#     print(f'Loaded {len(bounding_box_vertices)} bounding box vertices.')

#     for i in range(num_locations):
#         if not utils.isQueryInBoundingBox(bounding_box_vertices, query_lats[i], query_lons[i]):
#             return 'The query location, {query_lats[i]},{query_lons[i]},  is outside of the bounding box.', 400

#     # step 1, load up correction factors from file
#     # correction_factors = utils.loadCorrectionFactors(getenv("CORRECTION_FACTORS_FILENAME"))
#     # print(f'Loaded {len(correction_factors)} correction factors.')

#     # step 2, load up length scales from file
#     length_scales = utils.loadLengthScales()
#     print(f'Loaded {len(length_scales)} length scales.')

#     print('Loaded length scales:', length_scales, '\n')
#     length_scales = utils.getScalesInTimeRange(length_scales, query_start_datetime, query_end_datetime)
#     if len(length_scales) < 1:
#         msg = (
#             f"Incorrect number of length scales({len(length_scales)}) "
#             f"found in between {query_start_datetime} and {query_end_datetime}"
#         )
#         return msg, 400

#     latlon_length_scale = length_scales[0]['latlon']
#     elevation_length_scale = length_scales[0]['elevation']
#     time_length_scale = length_scales[0]['time']

#     print(
#         f'Using length scales: latlon={latlon_length_scale} elevation={elevation_length_scale} time={time_length_scale}'
#     )

#     # step 3, query relevent data

# # these conversions were when we were messing around with specifying radius in miles and so forth.      
# #    NUM_METERS_IN_MILE = 1609.34
# #    radius = latlon_length_scale / NUM_METERS_IN_MILE  # convert meters to miles for db query

# #    radius = latlon_length_scale / 70000


# # radius is in meters, as is the length scale and UTM.    
#     radius = SPACE_KERNEL_FACTOR_PADDING*latlon_length_scale

#     # sensor_data = request_model_data_local(
#     #     query_lats,
#     #     query_lons,
#     #     radius=radius,
#     #     start_date=query_start_datetime - timedelta(hours=TIME_KERNEL_FACTOR_PADDING*time_length_scale),
#     #     end_date=query_end_datetime + timedelta(hours=TIME_KERNEL_FACTOR_PADDING*time_length_scale))

#     # sensor_data = _requestFieldInRadius()

#     unique_sensors = {datum['ID'] for datum in sensor_data}
#     print(f'Loaded {len(sensor_data)} data points for {len(unique_sensors)} unique devices from bgquery.')

#     # step 3.5, convert lat/lon to UTM coordinates
#     try:
#         utils.convertLatLonToUTM(sensor_data)
#     except ValueError as err:
#         return f'{str(err)}', 400

#     sensor_data = [datum for datum in sensor_data if datum['zone_num'] == 12]

#     unique_sensors = {datum['ID'] for datum in sensor_data}
#     print("After removing points with zone num != 12: " + f"{len(sensor_data)} data points for {len(unique_sensors)} unique devices.")

#     # Step 4, parse sensor type from the version
#     sensor_source_to_type = {'AirU': '3003', 'PurpleAir': '5003', 'DAQ': '0000'}
# # DAQ does not need a correction factor
#     for datum in sensor_data:
#         datum['type'] =  sensor_source_to_type[datum['SensorSource']]

#     print(f'Fields: {sensor_data[0].keys()}')

#     # step 4.5, Data Screening
#     print('Screening data')
#     sensor_data = utils.removeInvalidSensors(sensor_data)

#     # # step 5, apply correction factors to the data
#     # for datum in sensor_data:
#     #     datum['PM2_5'] = utils.applyCorrectionFactor(correction_factors, datum['time'], datum['PM2_5'], datum['type'])

#     # step 6, add elevation values to the data
#     for datum in sensor_data:
#         if 'Altitude' not in datum:
#             datum['Altitude'] = elevation_interpolator([datum['Longitude']],[datum['Latitude']])[0]

#     # step 7, Create Model
#     model, time_offset = gaussian_model_utils.createModel(
#         sensor_data, latlon_length_scale, elevation_length_scale, time_length_scale)

#     # step 8, get estimates from model
#     query_dates = utils.interpolateQueryDates(query_start_datetime, query_end_datetime, query_rate)

#     # note - the elevation grid is the wrong way around, so you need to put in lons first
#     query_elevations = elevation_interpolator(query_lons, query_lats)
#     yPred, yVar = gaussian_model_utils.estimateUsingModel(
#         model, query_lats, query_lons, query_elevations, query_dates, time_offset)

#     num_times = len(query_dates)
#     estimates = []

#     for i in range(num_times):
#         estimates.append(
#             {'PM2_5': (yPred[:,i]).tolist(), 'variance': (yVar[:,i]).tolist(), 'datetime': query_dates[i].strftime('%Y-%m-%d %H:%M:%S%z'), 'Latitude': query_lats.tolist(), 'Longitude': query_lons.tolist(), 'Elevation': query_elevations.tolist()}
#             )

#     return jsonify(estimates)

#http://localhost:8080/api/getEstimateMap?lat_lo=40.644519&lon_lo=-111.971465&lat_hi=40.806852&lon_hi=-111.811118&lat_size=3&lon_size=3&date=2020-10-10T00:00:00Z
@app.route("/getEstimateMap", methods=["GET"], subdomain=getenv('SUBDOMAIN_API'))
@admin_utils.ingroup('admin')
@limiter.limit('1/minute')
def getEstimateMap():
    """
    lat_hi
    lat_lo
    lon_hi
    lon_lo
    lat_size
    lon_size
    date
    
    """
    # this species grid positions should be interpolated in UTM coordinates
    if "UTM" in request.args:
        UTM = True
    else:
        UTM = False

    # Get the arguments from the query string
    if not UTM:
        try:
            lat_hi = float(request.args.get('lat_hi'))
            lat_lo = float(request.args.get('lat_lo'))
            lon_hi = float(request.args.get('lon_hi'))
            lon_lo = float(request.args.get('lon_lo'))
        except ValueError:
            return 'lat, lon, lat_res, be floats in the lat-lon (not UTM) case', 400
        try:
            lat_size = int(request.args.get('lat_size'))
            lon_size = int(request.args.get('lon_size'))
        except ValueError:
            return 'lat, lon, sizes must be ints (not UTM) case', 400

        lat_res = (lat_hi-lat_lo)/float(lat_size)
        lon_res = (lon_hi-lon_lo)/float(lon_size)

    query_date = request.args.get('date')
    if not utils.validateDate(query_date):
        msg = f"Incorrect date format, should be {utils.DATETIME_FORMAT}, e.g.: 2018-01-03T20:00:00Z"
        return msg, 400

    query_datetime = utils.parseDateString(query_date)

    print((
        f"Query parameters: lat_lo={lat_lo} lat_hi={lat_hi}  lon_lo={lon_lo} lon_hi={lon_hi} lat_res={lat_res} lon_res={lon_res} date={query_datetime}"
    ))

    # step 0, load up the bounding box from file and check that request is within it
    bounding_box_vertices = utils.loadBoundingBox()
    print(f'Loaded {len(bounding_box_vertices)} bounding box vertices.')

    if not (
        utils.isQueryInBoundingBox(bounding_box_vertices, lat_lo, lon_lo) and
        utils.isQueryInBoundingBox(bounding_box_vertices, lat_lo, lon_hi) and
        utils.isQueryInBoundingBox(bounding_box_vertices, lat_hi, lon_hi) and
        utils.isQueryInBoundingBox(bounding_box_vertices, lat_hi, lon_lo)):
        return 'One of the query locations is outside of the bounding box for the database', 400

    # step 1, load up correction factors from file
    correction_factors = utils.loadCorrectionFactors()
    print(f'Loaded {len(correction_factors)} correction factors.')

    # step 2, load up length scales from file
    length_scales = utils.loadLengthScales()
    print(f'Loaded {len(length_scales)} length scales.')

    print('Loaded length scales:', length_scales, '\n')
    length_scales = utils.getScalesInTimeRange(length_scales, query_datetime, query_datetime)
    if len(length_scales) < 1:
        msg = (
            f"Incorrect number of length scales({len(length_scales)}) "
            f"found in between {query_datetime}-1day and {query_datetime}+1day"
        )
        return msg, 400

    latlon_length_scale = length_scales[0]['latlon']
    elevation_length_scale = length_scales[0]['elevation']
    time_length_scale = length_scales[0]['time']

    print(
        f'Using length scales: latlon={latlon_length_scale} elevation={elevation_length_scale} time={time_length_scale}'
    )

  # step 3, query relevent data
  # for this compute a circle center at the query volume.  Radius is related to lenth scale + the size fo the box.
    lat = (lat_lo + lat_hi)/2.0
    lon = (lon_lo + lon_hi)/2.0
#    NUM_METERS_IN_MILE = 1609.34
#    radius = latlon_length_scale / NUM_METERS_IN_MILE  # convert meters to miles for db query

    UTM_N_hi, UTM_E_hi, zone_num_hi, zone_let_hi = utils.latlonToUTM(lat_hi, lon_hi)
    UTM_N_lo, UTM_E_lo, zone_num_lo, zone_let_lo = utils.latlonToUTM(lat_lo, lon_lo)
# compute the length of the diagonal of the lat-lon box.  This units here are **meters**
    lat_diff = UTM_N_hi - UTM_N_lo
    lon_diff = UTM_E_hi - UTM_E_lo
    print(f'SPACE_KERNEL_FACTOR_PADDING= {type(SPACE_KERNEL_FACTOR_PADDING)}\nlatlon_length_scale={type(latlon_length_scale)}\nlat_diff={type(lat_diff)}\nlon_diff={type(lon_diff)}')
    radius = SPACE_KERNEL_FACTOR_PADDING*latlon_length_scale + np.sqrt(lat_diff**2 + lon_diff**2)/2.0

    if not ((zone_num_lo == zone_num_hi) and (zone_let_lo == zone_let_hi)):
        return 'Requested region spans UTM zones', 400        


#    radius = latlon_length_scale / 70000 + box_diag/2.0
    # sensor_data = request_model_data_local(
    #     lats=lat,
    #     lons=lon,
    #     radius=radius,
    #     start_date=query_datetime - TIME_KERNEL_FACTOR_PADDING*timedelta(hours=time_length_scale),
    #     end_date=query_datetime + TIME_KERNEL_FACTOR_PADDING*timedelta(hours=time_length_scale))
    
    # Convert dates to strings
    start = query_datetime - TIME_KERNEL_FACTOR_PADDING * timedelta(hours=time_length_scale)
    start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end = query_datetime + TIME_KERNEL_FACTOR_PADDING * timedelta(hours=time_length_scale)
    end_str = end.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f'Start: {start_str}\nEnd: {end_str}')

    sensor_data = _requestFieldInRadius(
        src_tbl=SRC_MAP['SLC'],
        field='PM2_5',
        lat=lat,
        lon=lon,
        radius=radius,
        start=start_str,
        end=end_str
    )

    # If it's a tuple then it returned a response code
    # TODO: Fix this, it is so weird. 
    if isinstance(sensor_data, tuple):
        return sensor_data

    # print("sensor data:", sensor_data)
    unique_sensors = {datum['DeviceID'] for datum in sensor_data}
    print(f'Loaded {len(sensor_data)} data points for {len(unique_sensors)} unique devices from bgquery.')

    # step 3.5, convert lat/lon to UTM coordinates
    try:
        utils.convertLatLonToUTM(sensor_data)
    except ValueError as err:
        return f'{str(err)}', 400

    # # Step 4, parse sensor type from the version
    # sensor_source_to_type = {'AirU': '3003', 'PurpleAir': '5003', 'DAQ': '0000'}
    # for datum in sensor_data:
    #     datum['type'] = sensor_source_to_type[datum['SensorSource']]

    # print(f'Fields: {sensor_data[0].keys()}')

    # step 4.5, Data Screening
    print('Screening data')
    sensor_data = utils.removeInvalidSensors(sensor_data)

    # Correction factors applied automatically in _requestFieldInRadius()
    #     # step 5, apply correction factors to the data
    # for datum in sensor_data:
    #     datum['PM2_5'] = utils.applyCorrectionFactor(correction_factors, datum['time'], datum['PM2_5'], datum['type'])

    # step 6, add elevation values to the data
    for datum in sensor_data:
        if 'Elevation' not in datum:
            datum['Elevation'] = elevation_interpolator([datum['Longitude']],[datum['Latitude']])[0]

    # step 7, Create Model
    model, time_offset = gaussian_model_utils.createModel(
        sensor_data, latlon_length_scale, elevation_length_scale, time_length_scale)

    
# step 8, build the grid of query locations
    if not UTM:

        # TODO: Create function interpolateQueryLocations()
        #   Tom Made one that might be correct
        lon_vector, lat_vector = utils.interpolateQueryLocations(lat_lo, lat_hi, lon_lo, lon_hi, lat_size, lon_size)
#        locations_UTM = utm.from_latlon(query_locations_latlon)
    else:
        # step 7.5, convert query box to UTM -- do the two far corners and hope for the best
#        lat_lo_UTM, lon_lo_UTM, zone_num_lo, zone_let_lo = utils.latlonToUTM(lat_lo, lon_lo)
#        lat_hi_UTM, lon_hi_UTM, zone_num_hi, zone_let_hi = utils.latlonToUTM(lat_hi, lon_hi)
#        query_locations_UTM = utils.interpolateQueryLocations(lat_lo_UTM, lat_hi_UTM, lon_lo_UTM, lon_hi_UTM, spatial_res)
#        query_locations_
        return 'UTM not yet supported', 400

    
#######################
# Ross was here
#######################

    # locations_lat = locations_lat.flatten()
    # locations_lon = locations_lon.flatten()
#    print(locations_lat.shape)
#    print(locations_lon.shape)
    elevations = elevation_interpolator(lon_vector, lat_vector)
    print(elevations.shape)

    locations_lon, locations_lat = np.meshgrid(lon_vector, lat_vector)
    # print("B")
    # print(locations_lat)
    # print(locations_lon)

    locations_lat = locations_lat.flatten()
    locations_lon = locations_lon.flatten()
    elevations = elevations.flatten()
    # print("C")
    # print(locations_lat)
    # print(locations_lon)

    # print("D")
    # print(locations_lat.reshape((lat_size, lon_size)))
    # print(locations_lon.reshape((lat_size, lon_size)))


    yPred, yVar = gaussian_model_utils.estimateUsingModel(
        model, locations_lat, locations_lon, elevations, [query_datetime], time_offset)

    elevations = (elevations.reshape((lat_size, lon_size))).tolist()
    yPred = yPred.reshape((lat_size, lon_size))
    yVar = yVar.reshape((lat_size, lon_size))
    estimates = yPred.tolist()
    variances = yVar.tolist()

    return jsonify({
            "Elevations": elevations, 
            "PM2.5": estimates, 
            "PM2.5 variance": variances, 
            "Latitudes": lat_vector.tolist(), 
            "Longitudes": lon_vector.tolist()
        })
