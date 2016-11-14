#!/usr/bin/python

import argparse
import datetime
import json
import subprocess
import sys

import numpy as np
import matplotlib.pyplot as plt


def exec_cmd(command):
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    output, error = process.communicate()

    return output, error


parser = argparse.ArgumentParser(
    description='Generate a graph decipting how long it took patches to get '
                'merged over time for a given user and project.')
parser.add_argument(
    'owner',
    help='The Gerrit username. For example amuller.')
parser.add_argument(
    'project',
    help='The OpenStack project to query. For example openstack/neutron.')
args = parser.parse_args()


def get_json_data_from_query(query):
    result, error = exec_cmd(
        'ssh -p 29418 review.openstack.org gerrit query %s --format=json' %
        query)

    if error:
        print error
        sys.exit(1)

    lines = result.split('\n')
    data = [json.loads(line) for line in lines[:-2]]

    if not data:
        print 'No patches found!'
        sys.exit(1)

    print 'Found metadata for %s patches' % len(data)
    data = sorted(data, key=lambda x: x['createdOn'])
    return data


def get_points_from_data(data):
    points = []

    for patch in data:
        creation = datetime.date.fromtimestamp(patch['createdOn'])
        updated = datetime.date.fromtimestamp(patch['lastUpdated'])
        x_value = (creation - start).days
        y_value = (updated - creation).days
        # Gerrit has a weird issue where some old patches have a bogus
        # createdOn value
        if y_value > 0:
            points.append((x_value, y_value))

    return points


def filter_above_percentile(points, percentile):
    percentile = np.percentile([point[1] for point in points], percentile)
    return [point for point in points if point[1] < percentile]


data = get_json_data_from_query(
    'status:merged branch:master owner:%(owner)s project:%(project)s' %
    {'owner': args.owner, 'project': args.project})

start = datetime.date.fromtimestamp(data[0]['createdOn'])

points = get_points_from_data(data)

points = filter_above_percentile(points, 95)

x = [point[0] for point in points]
y = [point[1] for point in points]

plt.xlabel(data[0]['owner']['name'])
plt.ylabel('Days to merge patch')
plt.grid()

# Generate a linear regression line
regression_line = np.polyfit(x, y, 1)
regression_line_function = np.poly1d(regression_line)

# Plot the data points as well as the regression line
plt.plot(x, y, '.', x, regression_line_function(x), '-')

x_axis = range(0, x[-1], max(1, x[-1] / 10))  # 0 to last point, 10 hops

# Generate a date from each hop relative to the date the first patch was
# contributed
x_axis_dates = [
    str(start + datetime.timedelta(days=day_delta)) for day_delta in x_axis]
plt.xticks(x_axis, x_axis_dates, rotation=45)

plt.show()
