from app.core.database import get_session_local
from app.models.article import Article
from app.services.kg.event_discovery import EventDiscoveryService as E
from datetime import datetime, timedelta
from urllib.parse import urlparse

db = get_session_local()()
cutoff = datetime.utcnow() - timedelta(days=90)
arts = db.query(Article).filter(
    Article.status.in_(["completed", "success"]), Article.scraped_at >= cutoff
).order_by(Article.scraped_at.desc()).limit(300).all()

svc = E()
coarse = {a.id: svc._tokens(f"{a.title} {a.summary}") for a in arts}

groups = []
assigned = set()
for a in sorted(arts, key=lambda x: x.scraped_at or datetime.min, reverse=True):
    if a.id in assigned:
        continue
    g = [a]
    assigned.add(a.id)
    for o in arts:
        if o.id in assigned:
            continue
        if svc._is_same_proposition(coarse[a.id], coarse[o.id]):
            g.append(o)
            assigned.add(o.id)
    groups.append(g)

print("groups with >1 article:")
for g in groups:
    if len(g) > 1:
        srcs = set()
        for x in g:
            net = (urlparse(x.url or "").netloc or "").lower().removeprefix("www.")
            srcs.add(net)
        print("  n=%d srcs=%s" % (len(g), srcs))
        for x in g[:6]:
            net = (urlparse(x.url or "").netloc or "").lower()
            print("    - [%s] %s" % (net, (x.title or "")[:32]))

print()
print("total groups:", len(groups), "multi:", sum(1 for g in groups if len(g) > 1))
db.close()