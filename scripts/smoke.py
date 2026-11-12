"""Runs against a running server. Intentionally approves the deterministic/replay test release."""
import argparse
import time
import httpx
p=argparse.ArgumentParser(); p.add_argument('--url',default='http://127.0.0.1:8000'); a=p.parse_args()
c=httpx.Client(base_url=a.url,timeout=10)
def wait(target, seconds=600):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        s=c.get('/api/state').json()
        if s['status'] in target: return s
        if s['status']=='failed': raise AssertionError(s)
        time.sleep(.5)
    raise AssertionError(f'Timeout waiting for {target}: {s}')
def post(path, **kw):
    r=c.post('/api/'+path,headers={'x-demo-token':token},**kw); r.raise_for_status(); return r.json()
s=wait({'healthy','resolved','rejected'})
assert s['mode'] in {'deterministic','replay'}, 'Smoke is restricted to non-billable modes'
token=c.get('/api/session').json()['token']
post('reset')
assert c.post('/api/inject').status_code==403
if s['mode']!='replay':
    assert c.post('/checkout',json={'subtotalCents':10000,'coupon':'SAVE10'}).json()['totalCents']==9000
    assert c.post('/checkout',json={'subtotalCents':10000}).status_code==500
post('inject')
s=wait({'awaiting_approval'})
assert s['patch'] and s['digest']
if s['mode']!='replay':
    assert c.post('/checkout',json={'subtotalCents':10000}).status_code==500, 'Must remain broken before approval'
r=c.post('/api/approve',headers={'x-demo-token':token},json={'incident':s['incident'],'digest':'wrong'})
assert r.status_code==409
post('approve',json={'incident':s['incident'],'digest':s['digest']})
wait({'resolved'})
assert c.post('/api/approve',headers={'x-demo-token':token},json={'incident':s['incident'],'digest':s['digest']}).status_code==409
if s['mode']!='replay':
    for coupon, total in [(None,10000),('SAVE10',9000),('',10000),('NOPE',10000)]:
        r=c.post('/checkout',json={'subtotalCents':10000,'coupon':coupon})
        assert r.status_code==200 and r.json()=={'totalCents':total,'currency':'EUR'}
    assert c.get('/api/metrics').status_code==200
with c.stream('GET','/api/events',headers={'Last-Event-ID':'1'}) as r:
    assert r.status_code==200
    first=next(line for line in r.iter_lines() if line.startswith('id:'))
    assert first=='id: 2', first
post('reset')
if s['mode']!='replay':
    assert c.post('/checkout',json={'subtotalCents':10000}).status_code==500
print('PASS: injection → detection → isolated repair → independent verification → approval → recovery → SSE resume → reset')
