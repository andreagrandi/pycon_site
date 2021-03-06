# -*- coding: UTF-8 -*-
import datetime
import json
from base64 import b64decode
from assopy.views import render_to_json
from collections import defaultdict
from conference import models as cmodels
from conference.utils import TimeTable2
from django import http
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

def _partner_as_event(fares):
    from conference.templatetags.conference import fare_blob
    partner = defaultdict(list)
    for f in fares:
        try:
            d = datetime.datetime.strptime(fare_blob(f, 'date'), '%Y/%m/%d').date()
            t = datetime.datetime.strptime(fare_blob(f, 'departure'), '%H:%M').time()
            dt = int(fare_blob(f, 'duration'))
        except Exception, e:
            continue
        partner[d].append({
            'duration': dt,
            'name': f['name'],
            'id': f['id'] * -1,
            'abstract': f['description'],
            'fare': f['code'],
            'schedule_id': None,
            'tags': set(['partner-program']),
            'time': datetime.datetime.combine(d, t),
            'tracks': ['partner0'],
        })
    return dict(partner)

def _build_timetables(schedules, events=None, partner=None):
    """
    Dati un elenco di schedule ids/eventi e partner program ritorna una lista
    di TimeTable relative ai dati passati.

    _build_timetables([1,2])

        Restituisce due TimeTable relative agli schedule 1 e 2 (gli eventi
        vengono recuperati dal db)

    _build_timetables([1,2], events=...)

        Restituisce due TimeTable relative agli schedule 1 e 2 usando solo gli
        eventi specificati (in questo caso events deve essere un dict che mappa
        all'id dello schedule una lista con gli id degli eventi).

    _build_timetables([1,2], partner=...)

        Restituisce almeno due TimeTable (altre potrebbero essere aggiunte a
        causa di partner program non coperti dagli schedule elencati).
        `partner` deve essere compatibile con l'output di `_partner_as_event`.
    """
    tts = []
    if schedules and not events:
        for row in schedules:
            tt = TimeTable2.fromSchedule(row['id'])
            tts.append((row['id'], tt))
    else:
        for row in schedules:
            tt = TimeTable2.fromEvents(row['id'], events[row['id']])
            tts.append((row['id'], tt))

    if partner:
        for date, evts in partner.items():
            for ix, row in enumerate(schedules):
                if row['date'] == date:
                    sid, tt = tts[ix]
                    break
            else:
                try:
                    sid = cmodels.Schedule.objects.get(date=date).id
                except cmodels.Schedule.DoesNotExist:
                    # it would be better to be able to show it anyway
                    continue
                tt = TimeTable2.fromEvents(sid, [])
                tts.append((sid, tt))
            for e in evts:
                e['schedule_id'] = sid
                tt.addEvents([e])
    def key(o):
        # timetable has an indirect reference to the day, I need to get it
        # from one of the events.
        tt = o[1]
        ev0 = tt.events.values()[0][0]
        return ev0['time']
    tts.sort(key=key)
    return tts

def _conference_timetables(conference):
    """
    Restituisce le TimeTable relative alla conferenza.
    """
    # The timetables must contain both events in the db and "artificial"
    # events from partner program
    sids = cmodels.Schedule.objects\
        .filter(conference=conference)\
        .values_list('id', flat=True)

    from conference.dataaccess import fares, schedules_data
    pfares = [ f for f in fares(conference) if f['ticket_type'] == 'partner' ]
    partner = _partner_as_event(pfares)

    schedules = schedules_data(sids)
    tts = _build_timetables(schedules, partner=partner)
    return tts

def schedule(request, conference):
    tts = _conference_timetables(conference)
    ctx = {
        'conference': conference,
        'sids': [ x[0] for x in tts ],
        'timetables': tts,
    }
    return render(request, 'p3/schedule.html', ctx)

def schedule_ics(request, conference, mode='conference'):
    if mode == 'my-schedule':
        if not request.user.is_authenticated():
            raise http.Http404()
        uid = request.user.id
    else:
        uid = None
    from p3.utils import conference2ical
    cal = conference2ical(conference, user=uid, abstract='abstract' in request.GET)
    return http.HttpResponse(list(cal.encode()), content_type='text/calendar')


def app_schedule_ics(request, conference):
    from p3.utils import conference2ical

    auth = request.META.get('HTTP_AUTHORIZATION', None)
    user_id = None

    if auth:
        from django.contrib.auth import authenticate
        basic, auth = auth.split()
        email, password = b64decode(auth).split(':')
        user = authenticate(email=email, password=password)
        if user is not None and user.is_active:
            user_id = user.id

    qs = cmodels.Event.objects.filter(
        schedule__conference=conference
    )

    stars = []
    if user_id:
        stars = qs.filter(
            eventinterest__interest__gt=0,
            eventinterest__user=user,
        ).values_list("pk", flat=True)

    events = []
    for e in qs:
        start, end = e.get_time_range()

        tracks = e.tracks.values_list("title", flat=True)
        track = tracks[0] if len(tracks) == 1 else "All Rooms"

        host = "https://www.pycon.it"

        abstract = e.talk.get_absolute_url() if e.talk else ""
        if abstract:
            abstract = "{}/{}".format(host, abstract)

        event = {
            "LOCATION": track,
            "UID": "{}/{}".format(host, e.pk),
            "CLASS": "PUBLIC",
            "DTSTART": start.strftime('%Y%m%dT%H%M%S'),
            "ORGANIZER;CN=Python Italia": "mailto:info@pycon.it",
            "DTEND": end.strftime('%Y%m%dT%H%M%S'),
            "GEO": "",
            "SUMMARY": e.__unicode__(),
            "ABSTRACT": abstract,
            "STAR": e.pk in stars,
            "LANGUAGE": e.talk.language if e.talk else 'en',
        }
        events.append(event)

    data = {
        "VCALENDAR":{
            "VERSION":"2.0",
            "VEVENT": events,
            "X-PUBLISHED-TTL":"P0DT1H0M0S",
            "PRODID":"https://www.pycon.it/en/sprints/schedule/pycon8/"
        }
    }

    return http.HttpResponse(json.dumps(data))


def schedule_list(request, conference):
    sids = cmodels.Schedule.objects\
        .filter(conference=conference)\
        .values_list('id', flat=True)
    ctx = {
        'conference': conference,
        'sids': sids,
        'timetables': zip(sids, map(TimeTable2.fromSchedule, sids)),
    }
    return render(request, 'p3/schedule_list.html', ctx)

@login_required
def jump_to_my_schedule(request):
    return redirect('p3-schedule-my-schedule', conference=settings.CONFERENCE_CONFERENCE)

@login_required
def my_schedule(request, conference):
    qs = cmodels.Event.objects\
        .filter(eventinterest__user=request.user, eventinterest__interest__gt=0)\
        .filter(schedule__conference=conference)\
        .values('id', 'schedule')

    events = defaultdict(list)
    for x in qs:
        events[x['schedule']].append(x['id'])

    qs = cmodels.EventBooking.objects\
        .filter(user=request.user, event__schedule__conference=conference)\
        .values('event', 'event__schedule')
    for x in qs:
        events[x['event__schedule']].append(x['event'])

    qs = cmodels.Ticket.objects\
        .filter(user=request.user)\
        .filter(fare__conference=conference, fare__ticket_type='partner')\
       .values_list('fare', flat=True)

    from conference.dataaccess import fares, schedules_data
    pfares = [ f for f in fares(conference) if f['id'] in qs ]
    partner = _partner_as_event(pfares)

    schedules = schedules_data(events.keys())
    tts = _build_timetables(schedules, events=events, partner=partner)
    ctx = {
        'conference': conference,
        'sids': [ x[0] for x in tts ],
        'timetables': tts,
    }
    return render(request, 'p3/my_schedule.html', ctx)

@render_to_json
def schedule_search(request, conference):
    from haystack.query import SearchQuerySet
    sqs = SearchQuerySet().models(cmodels.Event).auto_query(request.GET.get('q')).filter(conference=conference)
    return [ { 'pk': x.pk, 'score': x.score, } for x in sqs ]
