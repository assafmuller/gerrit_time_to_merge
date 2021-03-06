#!/usr/bin/python

import argparse
from collections import defaultdict
import datetime
import json
import os
import subprocess
import sys
import time

import numpy as np
import matplotlib.pyplot as plt
import pandas
from pystackalytics import Stackalytics


LOC_PERCENTILE = 75


def exec_cmd(command):
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    output, error = process.communicate()

    return output, error


parser = argparse.ArgumentParser(
    description='Generate graphs depicting how long it took patches to get '
                'merged over time for a given project or a subset of its '
                'contributors as a function of time, lines of code, number '
                'of reviews by the author and more. Note that the app uses '
                'a caching system - Query results are stored in the cache dir '
                'with no timeout. Subsequent runs of the app against the same '
                'project and set of contributors will not query Gerrit, but '
                'will use the local results. As the cache has no timeout, its '
                'contents must be deleted manually to get a fresh query.')
parser.add_argument(
    '--newer-than',
    help='Only look at patches merged in the last so and so days.')
parser.add_argument(
    '--verbose',
    action='store_true',
    help='Display author names, core status.')
parser.add_argument(
    'project',
    help='The OpenStack project to query. For example openstack/neutron.')
parser.add_argument(
    'owner',
    nargs='*',
    help='A list of zero or more Gerrit usernames. For example foo bar.')
args = parser.parse_args()


def _get_file_from_query(query):
    return query.replace('/', '_')


def get_json_data_from_cache(query):
    try:
        os.mkdir('cache')
    except OSError:
        pass

    query = _get_file_from_query(query)
    if query in os.listdir('cache'):
        with open('cache/%s' % query) as query_file:
            return json.load(query_file)


def put_json_data_in_cache(query, data):
    query = _get_file_from_query(query)
    with open('cache/%s' % query, 'w') as query_file:
        json.dump(data, query_file)


def get_json_data_from_query(query):
    data = []
    start = 0

    while True:
        gerrit_cmd = (
            'ssh -p 29418 review.openstack.org gerrit query --format=json '
            '--current-patch-set --start %(start)s %(query)s' %
            {'start': start,
             'query': query})
        result, error = exec_cmd(gerrit_cmd)

        if error:
            print(error)
            sys.exit(1)

        lines = result.split('\n')[:-2]
        data += [json.loads(line) for line in lines]

        if not data:
            print('No patches found!')
            sys.exit(1)

        print('Found metadata for %s more patches, %s total so far' %
              (len(lines), len(data)))
        start += len(lines)
        more_changes = json.loads(result.split('\n')[-2])['moreChanges']
        if not more_changes:
            break

    data = sorted(data, key=lambda x: x['createdOn'])
    return data


def get_submission_timestamp(patch):
    try:
        # Not all patches have approvals data
        approvals = patch['currentPatchSet']['approvals']
    except KeyError:
        return patch['lastUpdated']

    # Weirdly enough some patches don't have submission data.
    # Take lastUpdated instead.
    return next(
        (approval['grantedOn'] for approval in approvals if
         approval['type'] == 'SUBM'), patch['lastUpdated'])


def get_loc(patch):
    return max(0, patch['currentPatchSet']['sizeInsertions'] +
               patch['currentPatchSet']['sizeDeletions'])


def get_color(loc, max_loc):
    """Calculate a color between green and red.

    :param loc: How many lines of code?
    :param max_loc: The value of lines of code over which we return full red
    :return: (r, g, b) tuple
    """

    # Patches may have more LOC than the max we calculated,
    # for example 75th percentile.
    loc = min(loc, max_loc)
    return (loc / max_loc, 1.0 - (loc / max_loc), 0)


def get_average_loc(lines_of_code):
    return np.percentile(lines_of_code, LOC_PERCENTILE)


def get_points_from_data(data):
    def get_patch_author(patch):
        try:
            return patch['owner']['username']
        # Not all patches on Gerrit have an owner username interestingly enough
        except KeyError:
            return

    points = []

    start = datetime.date.fromtimestamp(data[0]['createdOn'])
    average_loc = get_average_loc([get_loc(patch) for patch in data])
    print('Lines of code %s percentile: %s' % (LOC_PERCENTILE, average_loc))

    for patch in data:
        creation = datetime.date.fromtimestamp(patch['createdOn'])
        submitted = datetime.date.fromtimestamp(
            get_submission_timestamp(patch))
        x_value = (creation - start).days
        y_value = (submitted - creation).days

        # Gerrit has a weird issue where some old patches have a bogus
        # createdOn value
        author = get_patch_author(patch)
        if y_value > 0 and author is not None:
            points.append({
                'date': x_value,
                'days_to_merge': y_value,
                'loc': get_loc(patch),
                'author': author})

    return points


def get_cores_from_data(data):
    # Dict from username of core to dict of min and max dates / actions as core
    cores = defaultdict(dict)
    start_date = int(time.time())
    end_date = 0

    for patch in data:
        if patch['createdOn'] < start_date:
            start_date = patch['createdOn']
        if patch['createdOn'] > end_date:
            end_date = patch['createdOn']

        try:
            # Not all patches have approvals data
            approvals = patch['currentPatchSet']['approvals']
        except KeyError:
            continue

        for approval in approvals:
            if(approval['type'] == 'Workflow' or
               (approval['type'] == 'Code-Review' and
               approval['value'] in ('-2', '2'))):
                date = approval['grantedOn']
                username = approval['by']['username']
                if not cores[username].get('min') or date < cores[username].get('min'):
                    cores[username]['min'] = date
                if not cores[username].get('max') or date > cores[username].get('max'):
                    cores[username]['max'] = date

    counts = []
    # {start|end}_date are in seconds since the epoch.
    # Iterate from start to end dates in jumps of days.
    for timestamp in range(start_date, end_date, 86400):
        count = 0
        for core, dates in cores.items():
            if dates['min'] <= timestamp <= dates['max']:
                count += 1
        counts.append(count)

    return counts


def get_list_of_owners(people):
    people_query = '\('
    for person in people:
        people_query += 'owner:%s OR ' % person
    return '%s\)' % people_query[:-4]


def moving_average(data, window):
    return pandas.Series(data).rolling(
        window=window, win_type='triang', min_periods=min(window, 10)).mean()


def set_fullscreen(fig):
    # http://stackoverflow.com/questions/12439588/
    # how-to-maximize-a-plt-show-window-using-python

    mng = fig.canvas.manager
    try:
        mng.frame.Maximize(True)
    except AttributeError:
        try:
            mng.window.showMaximized()
        except AttributeError:
            try:
                mng.resize(*mng.window.maxsize())
            except AttributeError:
                pass


def get_figure_title(prefix):
    owners = ' '.join(args.owner) + ' - ' if args.owner else ''
    return prefix + ' - ' + (owners + args.project).replace('/', '_')


def filter_top_5_percent_days_to_merge(points):
    percentile = np.percentile(
        [point['days_to_merge'] for point in points], 95)
    return [point for point in points if point['days_to_merge'] < percentile]


def calculate_time_to_merge_figure(points):
    fig, ax = plt.subplots()
    fig.canvas.set_window_title(get_figure_title('Time to merge'))

    points = filter_top_5_percent_days_to_merge(points)
    x = [point['date'] for point in points]
    y = [point['days_to_merge'] for point in points]

    print('Average days to merge patches: %s, median: %s' % (
          (int(round(np.average(y))), int(round(np.median(y))))))

    ax.set_xlabel('%s patches' % len(data))
    ax.set_ylabel('Days to merge patch')
    ax.grid(axis='y')

    window = min(len(x) / 10, 60)
    ax.plot(x, moving_average(y, window))

    average_loc = get_average_loc([get_loc(patch) for patch in data])
    colors = [get_color(point['loc'], average_loc) for point in points]

    def to_grey(r, g, b):
        return 0.21 * r + 0.72 * g + 0.07 * b

    size = [(1.0 - (to_grey(r, g, b))) * 70 for (r, g, b) in colors]
    ax.scatter(x, y, s=size, c=colors, alpha=0.75)

    x_axis = range(0, x[-1], max(1, x[-1] / 10))  # 0 to last point, 10 hops

    # Generate a date from each hop relative to the date the first patch was
    # contributed
    start = datetime.date.fromtimestamp(data[0]['createdOn'])
    x_axis_dates = [
        str(start + datetime.timedelta(days=day_delta))
        for day_delta in x_axis]
    ax.set_xticks(x_axis)
    ax.set_xticklabels(x_axis_dates, rotation=45)

    ax.set_xlim(xmin=-5)
    ax.set_ylim(ymin=-5)
    ax.legend(['Moving mean of the last %s patches' % window,
               'Lines of code, small & green to large & red'])
    fig.subplots_adjust(bottom=0.15)

    set_fullscreen(fig)


def calculate_time_to_merge_vs_number_of_patches(points):
    fig, ax = plt.subplots()
    fig.canvas.set_window_title(
        get_figure_title('Time to merge vs. number of patches'))

    points = filter_top_5_percent_days_to_merge(points)
    x = [point['date'] for point in points]
    y = [point['days_to_merge'] for point in points]

    ax.set_xlabel('%s patches' % len(data))
    ax.set_ylabel('Days to merge patch')
    ax.grid(b=False, axis='y')

    window = min(len(x) / 10, 60)
    l1 = ax.plot(x, moving_average(y, window))

    x_axis = range(0, x[-1], max(1, x[-1] / 10))  # 0 to last point, 10 hops

    # Generate a date from each hop relative to the date the first patch was
    # contributed
    start = datetime.date.fromtimestamp(data[0]['createdOn'])
    x_axis_dates = [
        str(start + datetime.timedelta(days=day_delta))
        for day_delta in x_axis]
    ax.set_xticks(x_axis)
    ax.set_xticklabels(x_axis_dates, rotation=45)

    # Plot a moving average of the number of patches per day
    ax2 = ax.twinx()
    ax2.grid(b=False)
    ax2.set_ylabel('Number of patches a day', color='r')
    y_n_patches = defaultdict(int)
    for point in points:
        y_n_patches[point['date']] += 1
    yy = []
    for date, count in sorted(y_n_patches.items()):
        yy.append(y_n_patches[date])
    patches_window = 30
    l2 = ax2.plot(list(set(x)), moving_average(yy, patches_window), color='r')

    ax3 = ax.twinx()
    ax3.grid(b=False)
    ax3.set_ylabel('Number of cores', color='g')
    core_counts = get_cores_from_data(data)
    l3 = ax3.plot(range(0, x[-1]), core_counts[0:x[-1]], color='g')

    ax.legend(
        l1 + l2 + l3,
        ('Moving mean of the time to merge of the last %s patches' % window,
         'Moving mean of the number of patches a day, with a window of '
         '%s days' % patches_window,
         'Number of cores'))
    fig.subplots_adjust(bottom=0.15)

    # Since we have two Y labels on the right, give them enough room
    fig.subplots_adjust(right=0.9)

    # Space out the Y labels of the second and third axes so they don't overlap
    ax2.spines['right'].set_position(('axes', 1.0))
    ax3.spines['right'].set_position(('axes', 1.05))
    set_fullscreen(fig)


def calculate_loc_correlation(points):
    fig, ax = plt.subplots()
    fig.canvas.set_window_title(get_figure_title('Lines of code'))

    percentile_time = np.percentile(
        [point['days_to_merge'] for point in points], 95)
    percentile_loc = np.percentile([point['loc'] for point in points], 95)
    points = [point for point in points if
              point['days_to_merge'] < percentile_time and
              point['loc'] < percentile_loc]

    x = [point['loc'] for point in points]
    y = [point['days_to_merge'] for point in points]

    ax.set_xlabel('Lines of code')
    ax.set_ylabel('Days to merge patch')

    ax.scatter(x, y, s=70, alpha=0.75)

    ax.set_xlim(xmin=-5)
    ax.set_ylim(ymin=-5)

    set_fullscreen(fig)


def get_days_to_merge_by_author(points):
    # A map from author to how many days it took to merge each of their patches
    authors = defaultdict(list)
    for point in points:
        authors[point['author']].append(point['days_to_merge'])
    return authors


def calculate_author_patches_time_to_merge(points):
    fig, ax = plt.subplots()
    fig.canvas.set_window_title(
        get_figure_title('Time to merge per author by commits'))

    points = filter_top_5_percent_days_to_merge(points)
    authors = get_days_to_merge_by_author(points)

    x = []
    y = []
    labels = []
    for author, patches in authors.items():
        x.append(len(patches))  # How many patches
        # The average of how long it took to merge the patches
        y.append(np.average(patches))
        labels.append(author)

    ax.set_xlabel('Patches by author')
    ax.set_ylabel('Average days to merge patch per author')
    ax.scatter(x, y, s=70, alpha=0.75)

    if args.verbose:
        for i in range(0, len(labels)):
            plt.annotate(labels[i], (x[i], y[i]))

    ax.set_xlim(-5, max(x) + 5)
    ax.set_ylim(-5, max(y) + 5)

    set_fullscreen(fig)


def calculate_author_time_to_merge_histogram(points):
    fig, ax = plt.subplots()
    fig.canvas.set_window_title(
        get_figure_title('Time to merge per author distribution'))

    points = filter_top_5_percent_days_to_merge(points)
    authors = get_days_to_merge_by_author(points)

    x = []
    for author, patches in authors.items():
        if len(patches) >= 10:
            # The average of how long it took to merge the patches
            x.append(np.average(patches))

    n, bins, patches = ax.hist(x, alpha=0.5)

    ax.set_xticks(bins)
    ax.set_xlabel('Days to merge patches')
    ax.set_ylabel('Amount of authors with 10 or more patches')

    set_fullscreen(fig)


def calculate_author_reviews_time_to_merge(points):
    _calculate_author_time_to_merge_by_metric(points, 'marks')


def calculate_author_emails_time_to_merge(points):
    _calculate_author_time_to_merge_by_metric(points, 'emails')


def calculate_author_filed_bugs_time_to_merge(points):
    _calculate_author_time_to_merge_by_metric(points, 'filed-bugs')


def calculate_author_resolved_bugs_time_to_merge(points):
    _calculate_author_time_to_merge_by_metric(points, 'resolved-bugs')


def calculate_author_drafted_blueprints_time_to_merge(points):
    _calculate_author_time_to_merge_by_metric(points, 'bpd')


def calculate_author_implemented_blueprints_time_to_merge(points):
    _calculate_author_time_to_merge_by_metric(points, 'bpc')


def _is_core(author):
    return author['core'] == 'master'


def _get_color_by_core(is_core):
    return (
        0, 1 if is_core else 0, 0 if is_core else 1) if \
        args.verbose else (0, 0, 1)


def _calculate_author_time_to_merge_by_metric(points, metric):
    METRIC_LABELS = {
        'marks': 'Reviews',
        'emails': 'Emails',
        'bpd': 'Drafted Blueprints',
        'bpc': 'Completed Blueprints',
        'filed-bugs': 'Filed Bugs',
        'resolved-bugs': 'Resolved Bugs'}

    fig, ax = plt.subplots()
    fig.canvas.set_window_title(
        get_figure_title(
            'Time to merge per author by %s') % METRIC_LABELS[metric])

    points = filter_top_5_percent_days_to_merge(points)
    authors = get_days_to_merge_by_author(points)

    stackalytics = Stackalytics()
    module = args.project.split('/')[-1]

    if metric == 'emails':
        # When retrieving via emails it looks like Stackalytics tries to
        # find only emails with [module] in the title
        module = None

    stackalytics_args = {
        'module': module,
        'release': 'all',
        'metric': metric}
    if args.newer_than:
        # Now - newer_than days
        stackalytics_args['start_date'] = \
            int(time.time() - int(args.newer_than) * 86400)
    s_result = stackalytics.engineers(**stackalytics_args)['stats']

    if not s_result:
        error = 'No result found from Stackalytics API for module %s and ' \
                'metric %s' % (module, metric)
        print(error)
        return

    s_result_by_author = {}
    for item in s_result:
        s_result_by_author[item['id']] = {
            'metric': item['metric'],
            'core': item['core']}

    colors = []
    labels = []
    x = []
    y = []
    for author, patches in sorted(authors.items()):
        try:
            x.append(s_result_by_author[author]['metric'])
        # People don't always use the same Gerrit and Stackalytics/Launchpad
        # user IDs.
        except KeyError:
            continue

        labels.append(author)
        colors.append(_get_color_by_core(_is_core(s_result_by_author[author])))
        # The average of how long it took to merge the patches
        y.append(np.average(patches))

    if not x:
        print('Could not find results for %s by %s' % (authors.keys(), metric))
        return

    ax.set_xlabel('%s by author' % METRIC_LABELS[metric])
    ax.set_ylabel('Average days to merge patch per author')

    ax.scatter(x, y, s=70, alpha=0.75, c=colors)

    if args.verbose:
        for i in range(0, len(labels)):
            ax.annotate(labels[i], (x[i], y[i]))

    ax.set_xlim(0, max(x) + 5)
    ax.set_ylim(0, max(y) + 5)

    set_fullscreen(fig)


query = "status:merged branch:master project:%s " % args.project
if args.owner:
    query += get_list_of_owners(args.owner)
if args.newer_than:
    query += ' -- -age:%dd' % int(args.newer_than)

print(query)
data = get_json_data_from_cache(query)
if not data:
    data = get_json_data_from_query(query)
    put_json_data_in_cache(query, data)

points = get_points_from_data(data)

if not points:
    error = 'Could not parse points from data. It is likely that the ' \
            'createdOn timestamp of the patches found is bogus.'
    print(error)
    sys.exit(1)

plt.style.use('fivethirtyeight')

if args.newer_than:
    print('Looking at patches and Stackalytics metrics newer than %s days, '
          'not showing the moving mean graph' % args.newer_than)
else:
    calculate_time_to_merge_figure(points)

calculate_time_to_merge_vs_number_of_patches(points)
calculate_loc_correlation(points)
calculate_author_patches_time_to_merge(points)
calculate_author_reviews_time_to_merge(points)
calculate_author_emails_time_to_merge(points)
calculate_author_filed_bugs_time_to_merge(points)
calculate_author_resolved_bugs_time_to_merge(points)
calculate_author_drafted_blueprints_time_to_merge(points)
calculate_author_implemented_blueprints_time_to_merge(points)
calculate_author_time_to_merge_histogram(points)
plt.show()
