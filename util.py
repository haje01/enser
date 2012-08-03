from functools import wraps
from werkzeug.utils import redirect
import time

SESSION_TIMEOUT = 60 * 60 * 10

def is_session_timeout(request):
    login_ts = request.client_session.get('login_ts', None)
    r = not login_ts or time.time() - login_ts > SESSION_TIMEOUT
    return r

def is_version_match(request):
    return True

def is_valid_auth(request):
    return not is_session_timeout(request) and is_version_match(request)

def require_auth(f):
    @wraps(f)
    def decorated(stderr, request, **kw):
        if is_valid_auth(request):
            return f(stderr, request, **kw)
        else:
            url = '%s?next=%s' % ('/home', request.path)
            return redirect(url)
    return decorated
