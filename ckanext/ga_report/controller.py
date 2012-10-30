import re
import logging
import operator
import collections
from ckan.lib.base import BaseController, c, render, request, response, abort

import sqlalchemy
from sqlalchemy import func, cast, Integer
import ckan.model as model
from ga_model import GA_Url, GA_Stat

log = logging.getLogger('ckanext.ga-report')


def _get_month_name(strdate):
    import calendar
    from time import strptime
    d = strptime(strdate, '%Y-%m')
    return '%s %s' % (calendar.month_name[d.tm_mon], d.tm_year)


def _month_details(cls):
    months = []
    vals = model.Session.query(cls.period_name).distinct().all()
    for m in vals:
        months.append( (m[0], _get_month_name(m[0])))
    return sorted(months, key=operator.itemgetter(0), reverse=True)


class GaReport(BaseController):

    def csv(self, month):
        import csv

        q = model.Session.query(GA_Stat)
        if month != 'all':
            q = q.filter(GA_Stat.period_name==month)
        entries = q.order_by('GA_Stat.period_name, GA_Stat.stat_name, GA_Stat.key').all()

        response.headers['Content-Type'] = "text/csv; charset=utf-8"

        writer = csv.writer(response)
        writer.writerow(["Period", "Statistic", "Key", "Value"])

        for entry in entries:
            writer.writerow([entry.period_name.encode('utf-8'),
                             entry.stat_name.encode('utf-8'),
                             entry.key.encode('utf-8'),
                             entry.value.encode('utf-8')])

    def index(self):

        # Get the month details by fetching distinct values and determining the
        # month names from the values.
        c.months = _month_details(GA_Stat)

        # Work out which month to show, based on query params of the first item
        c.month_desc = 'all time'
        c.month = request.params.get('month', '')
        if c.month:
            c.month_desc = ''.join([m[1] for m in c.months if m[0]==c.month])

        q = model.Session.query(GA_Stat).\
            filter(GA_Stat.stat_name=='Totals')
        if c.month:
            q = q.filter(GA_Stat.period_name==c.month)
        entries = q.order_by('ga_stat.key').all()

        def clean_key(key, val):
            if key in ['Average time on site', 'Pages per visit', 'Percent new visits']:
                val =  "%.2f" % round(float(val), 2)
                if key == 'Average time on site':
                    mins, secs = divmod(float(val), 60)
                    hours, mins = divmod(mins, 60)
                    val = '%02d:%02d:%02d (%s seconds) ' % (hours, mins, secs, val)
                key = '%s *' % key
            if key in ['Bounces', 'Total pageviews']:
                val = int(val)
            return key, val

        c.global_totals = []
        if c.month:
            for e in entries:
                key, val = clean_key(e.key, e.value)
                c.global_totals.append((key, val))
        else:
            d = collections.defaultdict(list)
            for e in entries:
                d[e.key].append(float(e.value))
            for k, v in d.iteritems():
                if k in ['Bounces', 'Total pageviews']:
                    v = sum(v)
                else:
                    v = float(sum(v))/len(v)
                key, val = clean_key(k,v)
                c.global_totals.append((key, val))
                c.global_totals = sorted(c.global_totals, key=operator.itemgetter(0))

        keys = {
            'Browser versions': 'browsers',
            'Operating Systems versions': 'os',
            'Social sources': 'social_networks',
            'Languages': 'languages',
            'Country': 'country'
        }

        browser_version_re = re.compile("(.*)\((.*)\)")
        for k, v in keys.iteritems():

            def clean_field(key):
                if k != 'Browser versions':
                    return key
                m = browser_version_re.match(key)
                browser = m.groups()[0].strip()
                ver = m.groups()[1]
                parts = ver.split('.')
                if len(parts) > 1:
                    if parts[1][0] == '0':
                        ver = parts[0]
                    else:
                        ver = "%s.%s" % (parts[0],parts[1])
                if browser in ['Safari','Android Browser']:  # Special case complex version nums
                    ver = parts[0]
                    if len(ver) > 2:
                        ver = "%s%sX" % (ver[0], ver[1])

                return "%s (%s)" % (browser, ver,)

            q = model.Session.query(GA_Stat).\
                filter(GA_Stat.stat_name==k)
            if c.month:
                entries = []
                q = q.filter(GA_Stat.period_name==c.month).\
                          order_by('ga_stat.value::int desc')

            d = collections.defaultdict(int)
            for e in q.all():
                d[clean_field(e.key)] += int(e.value)
            entries = []
            for key, val in d.iteritems():
                entries.append((key,val,))
            entries = sorted(entries, key=operator.itemgetter(1), reverse=True)

            setattr(c, v, [(k,v) for k,v in entries ])



        return render('ga_report/site/index.html')


class GaPublisherReport(BaseController):
    """
    Displays the pageview and visit count for specific publishers based on
    the datasets associated with the publisher.
    """

    def index(self):

        # Get the month details by fetching distinct values and determining the
        # month names from the values.
        c.months = _month_details(GA_Url)

        # Work out which month to show, based on query params of the first item
        c.month = request.params.get('month', '')
        c.month_desc = 'all time'
        if c.month:
            c.month_desc = ''.join([m[1] for m in c.months if m[0]==c.month])

        connection = model.Session.connection()
        q = """
            select department_id, sum(pageviews::int) views, sum(visitors::int) visits
            from ga_url
            where department_id <> ''"""
        if c.month:
            q = q + """
                    and period_name=%s
            """
        q = q + """
                group by department_id order by views desc limit 20;
            """

        # Add this back (before and period_name =%s) if you want to ignore publisher
        # homepage views
        # and not url like '/publisher/%%'

        c.top_publishers = []
        res = connection.execute(q, c.month)

        for row in res:
            c.top_publishers.append((model.Group.get(row[0]), row[1], row[2]))

        return render('ga_report/publisher/index.html')


    def read(self, id):
        count = 20

        c.publisher = model.Group.get(id)
        if not c.publisher:
            abort(404, 'A publisher with that name could not be found')
        c.top_packages = [] # package, dataset_views in c.top_packages

        # Get the month details by fetching distinct values and determining the
        # month names from the values.
        c.months = _month_details(GA_Url)

        # Work out which month to show, based on query params of the first item
        c.month = request.params.get('month', '')
        if not c.month:
            c.month_desc = 'all time'
        else:
            c.month_desc = ''.join([m[1] for m in c.months if m[0]==c.month])

        c.publisher_page_views = 0
        q = model.Session.query(GA_Url).\
            filter(GA_Url.url=='/publisher/%s' % c.publisher.name)
        if c.month:
            entry = q.filter(GA_Url.period_name==c.month).first()
            c.publisher_page_views = entry.pageviews if entry else 0
        else:
            for e in q.all():
                c.publisher_page_views = c.publisher_page_views  + int(e.pageviews)


        q =  model.Session.query(GA_Url).\
            filter(GA_Url.department_id==c.publisher.name).\
            filter(GA_Url.url.like('/dataset/%'))
        if c.month:
            q = q.filter(GA_Url.period_name==c.month)
        q = q.order_by('ga_url.pageviews::int desc')

        if c.month:
            for entry in q[:count]:
                p = model.Package.get(entry.url[len('/dataset/'):])
                c.top_packages.append((p,entry.pageviews,entry.visitors))
        else:
            ds = {}
            for entry in q.all():
                if len(ds) >= count:
                    break
                p = model.Package.get(entry.url[len('/dataset/'):])
                if not p in ds:
                    ds[p] = {'views':0, 'visits': 0}
                ds[p]['views'] = ds[p]['views'] + int(entry.pageviews)
                ds[p]['visits'] = ds[p]['visits'] + int(entry.visitors)

            results = []
            for k, v in ds.iteritems():
                results.append((k,v['views'],v['visits']))

            c.top_packages = sorted(results, key=operator.itemgetter(1), reverse=True)

        return render('ga_report/publisher/read.html')
