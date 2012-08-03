#!/usr/bin/env python
# encoding=utf-8

import os   
import redis
from werkzeug.wrappers import BaseRequest, Response
from werkzeug.routing import Map, Rule
from werkzeug.exceptions import HTTPException, NotFound
from werkzeug.wsgi import SharedDataMiddleware
from werkzeug.utils import redirect
from jinja2 import Environment, FileSystemLoader
import time
import logging
from logging import handlers
import sys
from nltk.corpus import brown
import nltk
from xml.sax.saxutils import escape
from multiprocessing import Process
import zmq
from werkzeug.utils import redirect, cached_property
from werkzeug.contrib.securecookie import SecureCookie
import util
from util import require_auth
import re
import types
import argparse
import urllib
from bs4 import BeautifulSoup
import urllib2
import en

USE_GEVENT = True
if USE_GEVENT:
    from gevent import monkey; monkey.patch_all()
    from gevent.wsgi import WSGIServer

MAX_LOG_BYTES = 5000000
MAX_LOG_BACKUPS = 5
MAX_RECENT_QUERIES = 4
MAX_PASS = 7
SENT_PER_PAGE = 10
STICKY_CHARS = (',', '.', ':', '?', '`', '!', "'")
SKIP_CHARS = ('"', '""', "'", "''", '`', '``', '(', ')', '.', ':', ';',
        '-', '--')
COOKIE_SECRET = '\x08\xd1v\xb1\xb4Ui \xa1T\xe0\x88\x0b\xc6F\xdd\xee)w\xe9'

DEV_MODE = True # eval(os.environ['PROJ_DEV'])
DAEMON_MODE = False # eval(os.environ['PROJ_DAEMON'])
SERVICE_NAME= "enser" # os.environ['PROJ_SERVICE_NAME']
MSG_LIST_PTRN = re.compile(r'(\[([^]]*)\])')
DIC_REQ_URL="http://endic.naver.com/popManager.nhn?m=search&searchOption=entry_idiom&query=%s"
MODAL_VERBS = ['will', 'would', 'shall', 'should', 'can', 'could']

work_sender = None
result_receiver = None
categories = []
category_sentences = {}
senses = {}
senses['$place'] = "[london, newyork, tokyo, seoul, canada, usa, america, here, house, home, bed, school]"
senses['$time'] = "[monday, tuesday, wendnesday, thursday, friday, saturday,\
sunday, morning, afternoon, evening, night, late, early, january, february,\
march, april, may, june, july, august, september, november, december]"
senses['$people_sbj'] = "[I, you, he, she, they]"
senses['$people_obj'] = "[me, you, him, her, them]"

custom_verb_tense = {}
custom_verb_tense['get'] = ('got', 'gotten')


class Request(BaseRequest):
    @cached_property
    def client_session(self):
        return SecureCookie.load_cookie(self, secret_key=COOKIE_SECRET)

def init_logger(args):
    global log, logger_name
    logger_name = 'enser_logger_%d' % args.server_port
    log = logging.getLogger(logger_name)
    log.setLevel(logging.DEBUG if DEV_MODE else logging.INFO )

    lformat = logging.Formatter("%(asctime)s [%(levelname)s] - %(message)s")

    if not DAEMON_MODE:
        conh = logging.StreamHandler()
        log.addHandler(conh)
        conh.setFormatter(lformat)

        class ErrorWriter(object):
            def write(self, msg):
                log = logging.getLogger(logger_name)
                log.error(msg)
            def flush(self):
                pass

        sys.stderr.flush()
        sys.stderr = ErrorWriter()

    fileh = handlers.RotatingFileHandler('logs/enser_%d.log' %
    args.server_port, maxBytes = MAX_LOG_BYTES, backupCount = MAX_LOG_BACKUPS)
    log.addHandler(fileh)
    fileh.setFormatter(lformat)

def init_corpus():
    print 'init corpus.. ', 
    global categories, category_sentences
    categories = brown.categories()
    for category in categories:
        sents = brown.tagged_sents(categories = category)
        category_sentences[category] = sents
    print 'done'

def find_category_sentences(category, show_tag, target_words, target_tags):
    results = []
    sent_cnt = 0
    for sentence in category_sentences[category]:
        sent_cnt += 1
        found, result, _ = check_senetence(sentence, show_tag, target_words, target_tags)
        if found:
            results.append(''.join(result))

    return sent_cnt, results
            
def check_senetence(sentence, show_tag, target_words, target_tags):
    goal = len(target_words)
    match_idx = pass_cnt = 0
    found = passone = passmany = match_begin = False
    result = []
    def append_result(result, word, prev_match = 0):
        if word not in STICKY_CHARS:
            w = ' ' + escape(word)
        else:
            w = escape(word)
        if prev_match > 0:
            result.insert(-prev_match, w)
        else:
            result.append(w)

    def next_target(idx, target_words, target_tags):
        word = target_words[idx]
        tag = target_tags[idx]
        def next_word(word):
            prestar = poststar = False
            if word not in ('*', '**'):
                if word.startswith('*'):
                    prestar = True
                    word = word.lstrip('*')
                if word.endswith('*'):
                    poststar = True
                    word = word.rstrip('*')
            return word, prestar, poststar

        def is_major(word):
            return en.is_verb(word) or en.is_adjective(word) or\
            en.is_adverb(word) or (word in MODAL_VERBS)

        if type(word) == types.ListType:
            words = []
            tags = []
            majors = []
            prestars = []
            poststars = []
            lidx = 0
            for w in word:
                ltag = tag[lidx]
                lword, prestar, poststar = next_word(w)
                words.append(lword)
                if lword.endswith('@'):
                    lword = lword[:-1]
                tags.append(ltag)
                if is_major(lword):
                    majors.append(True)
                else:
                    majors.append(False)
                prestars.append(prestar)
                poststars.append(poststar)
                lidx += 1
            return idx + 1, words, tags, majors, prestars, poststars
        else:
            word, prestar, poststar = next_word(word)
            if word.endswith('@'):
                major = is_major(word[:-1])
            else:
                major = is_major(word)
        return idx + 1, word, tag, major, prestar, poststar

    def word_match(word, target_word, prestar, poststar):
        word = word.lower()
        if word == target_word:
            return True
        if target_word.endswith('@'):
            verb = target_word[:-1]
            if verb in custom_verb_tense:
                past = custom_verb_tense[verb][0]
            else:
                past = en.verb.past(verb)
            if verb in custom_verb_tense:
                past_participle = custom_verb_tense[verb][1]
            else:
                past_participle = en.verb.past_participle(verb)
            if word == verb or word == past or word == past_participle:
                return True
        return (prestar and word.endswith(target_word)) or (poststar and 
        word.startswith(target_word))

    match_idx, target_word, target_tag, target_major, prestar, poststar = \
    next_target(match_idx, target_words, target_tags)

    def check_word_and_tag(target_word, target_tag, target_major, word, tag, prestar, poststar, passone, passmany):
        def check(target_word, target_tag, target_major, word, tag, prestar, poststar,
        passone, passmany):
            major_match = is_match = False
            match_word = word
            if not target_tag or tag.startswith(target_tag):
                if target_word == '*':
                    passone = True
                elif target_word == '**':
                    passone = passmany = True
                elif not target_word or word_match(word, target_word, prestar, poststar):
                    is_match = True
                    if target_major:
                        major_match = True
                    passmany = False
            return is_match, passone, passmany, match_word, major_match

        if type(target_tag) == types.ListType:
            for target_w, target_t, target_v, pre, post in zip(target_word,
                target_tag, target_major, prestar, poststar):
                is_match, passone, passmany, match_word, major_match = check(target_w, target_t, 
                target_v, word, tag, pre, post, passone, passmany)
                if is_match:
                    return is_match, passone, passmany, match_word, major_match
            return False, passone, passmany, word, major_match
        else:
            return check(target_word, target_tag, target_major, word, tag, prestar, poststar,
            passone, passmany)

    prev_match = 0
    major_match = False
    for word, tag in sentence:
        is_match = False
        if target_word or target_tag:
            is_match, passone, passmany, match_word, _major_match = check_word_and_tag(target_word, \
            target_tag, target_major, word, tag, prestar, poststar, passone, passmany)
            if _major_match:
                major_match = True

        if not found and match_begin and word in SKIP_CHARS:
            return None, None, None

        if is_match or passone or (passmany and pass_cnt < MAX_PASS):
            word = escape(word)
            pass_cnt += 1
            if not match_begin and passone:
                prev_match += 1
            if is_match:
                passmany = False
                pass_cnt = 0
                if not match_begin:
                    append_result(result, '<strong>', prev_match)
                    match_begin = True
            if show_tag:
                append_result(result, match_word + ('<sub style="color: gray">%s</sub>' %
                tag.lower() if show_tag else ''))
            else:
                append_result(result, match_word)
            if (is_match or passone) and match_idx == goal:
                append_result(result, '</strong>')
                found = True
                match_begin = False
                target_word = target_tag = target_major = None
            elif match_idx < goal and is_match or passone:
                match_idx, target_word, target_tag, target_major, prestar, poststar = \
                next_target(match_idx, target_words, target_tags)
            passone = False
        else:
            if match_begin and not is_match:
                return None, None, None
            elif passmany and pass_cnt >= MAX_PASS:
                return None, None, None
            else:
                append_result(result, word + ('<sub style="color:\
                gray">%s</sub>' % tag.lower() if show_tag else ''))
    if found:
        return True, result, major_match
    return False, None, major_match

class Application(object):
    def __init__(self, args):
        self.db = redis.Redis(args.redis_ip, args.redis_port)
        template_path = os.path.join(os.path.dirname(__file__), 'templates')
        self.jinja_env = Environment(loader=FileSystemLoader(template_path),
        autoescape=False)
        self.url_map = Map([
            Rule('/', endpoint='default'),
            Rule('/home', endpoint='home'),
            Rule('/write', endpoint='write'),
            Rule('/write/check/<sent>', endpoint='write_check'),
            Rule('/corpus/search', endpoint='corpus_search'),
            Rule('/query', endpoint='query'),
            Rule('/grammar/register', endpoint='grammar_register'),
            Rule('/grammar/register/submit', endpoint='grammar_register'),
            Rule('/grammar/edit/<int:gid>', endpoint='grammar_edit'),
            Rule('/grammar/edit/<int:gid>/submit', endpoint='grammar_edit'),
            Rule('/grammar/view', endpoint='grammar_view'),
            Rule('/grammar/view/<int:gid>', endpoint='grammar_view'),
            Rule('/grammar/delete/<int:gid>', endpoint='grammar_delete'),
            Rule('/todo', endpoint='todo'),
            Rule('/join', endpoint='join'),
            Rule('/dictionary/<msg>', endpoint='dictionary'),
            Rule('/login', endpoint='login'),
            Rule('/logout', endpoint='logout'),
            Rule('/test', endpoint='test'),
        ])

    def render_template(self, request, template_name, **context):
        t = self.jinja_env.get_template(template_name)
        context['cur_path'] = request.path
        context['url_root'] = request.url_root[:-1]
        context['sent_per_page'] = SENT_PER_PAGE
        if util.is_valid_auth(request):
            nid = request.client_session['nid']
            context['recent_queries'] = self.db.lrange('user:%s:recent_queries' % nid, 0, 3)
            auth_id = request.client_session['auth_id'].decode('utf-8')
            context['auth_id'] = auth_id
            context['auth_nid'] = nid
        return Response(t.render(context), mimetype='text/html')

    def dispatch_request(self, request):
        adapter = self.url_map.bind_to_environ(request.environ)
        try:
            endpoint, values = adapter.match()
            return getattr(self, 'on_' + endpoint)(request, **values)
        except HTTPException, e:
            return e

    def wsgi_app(self, environ, start_response):
        request = Request(environ)
        response = self.dispatch_request(request)
        if request.client_session.should_save:
            request.client_session.save_cookie(response)
        return response(environ, start_response)

    def __call__(self, environ, start_response):
        return self.wsgi_app(environ, start_response)

    def on_default(self, request):
        return redirect('/home')

    def on_home(self, request):
        return self.render_template(request, 'home.html')

    @require_auth
    def on_write(self, request):
        return self.render_template(request, 'write.html')

    @require_auth
    def on_write_check(self, request, sent):
        sent = sent.strip('"')
        text_and_tag = nltk.pos_tag(nltk.word_tokenize(sent))
        print text_and_tag
        last_gid = int(self.db.get('grammar_last_nid'))
        gid = 0
        results = []
        for i in range(3):
            results.append([])

        def check_query(query, idx):
            target_words, target_tags = collect_words_and_tags(query)
            found, result, major_match = check_senetence(text_and_tag, False, target_words, target_tags)
            print query, found, result, major_match
            if found:
                results[idx].append((gid, name, desc, name))
                return True
            elif major_match:
                results[2].append((gid, name, desc, name))
                return True

        while gid <= last_gid:
            name = self.db.get('grammar:%d:name' % gid)
            if name:
                appended = False
                desc = self.db.get('grammar:%d:desc' % gid)
                good_queries = self.db.lrange('grammar:%d:good_queries' % gid,
                0, -1)
                for gq in good_queries:
                    appended = check_query(gq, 0)
                    if appended:
                        break
                if not appended:
                    error_queries = self.db.lrange('grammar:%d:error_queries' % gid,
                    0, -1)
                    for eq in error_queries:
                        if check_query(eq, 1):
                            break
            gid += 1

        xml = '<results count="%d">' % len(results)
        idx = 0
        for _type in range(3):
            for gid, name, desc, name in results[_type]:
                if _type == 0:
                    cls = "btn-primary"
                elif _type == 1:
                    cls = "btn-danger"
                elif _type == 2:
                    cls = "btn-success"
                xml += '<grammar id="grammar-%d" gid="%d" class="%s"\
                name="%s"><![CDATA[%s]]></grammar>' % (idx, gid, cls, name, desc)
                idx += 1
        xml += '</results>'
        return Response(xml, mimetype='text/xml')

    @require_auth
    def on_corpus_search(self, request):
        nid = request.client_session['nid']
        return self.render_template(request, 'corpus_search.html')

    def on_query(self, request):
        start = time.time()
        msg = request.args['msg'].strip('"').strip().lower()
        show_tag = request.args['show_tag']

        for category in categories:
            work_sender.send_unicode(category + ':' + show_tag + ' ' + msg)

        work_results = []
        total_sent_cnt = 0
        for category in categories:
            res = result_receiver.recv_pyobj()
            total_sent_cnt += int(res[0])
            if res:
                work_results.extend(res[1:])

        match_cnt = len(work_results)
        page_cnt = round(len(work_results) / float(SENT_PER_PAGE))
        page_no = 1
        cnt = 0
        result = '<sentences total_cnt="%d" match_cnt="%d" page_cnt="%d"\
        elapsed="%f"><page no="1">' %\
        (total_sent_cnt, match_cnt, page_cnt, time.time() - start)
        for res in work_results:
            if cnt >= SENT_PER_PAGE:
                page_no += 1
                result += '</page><page no="%d">' % page_no
                cnt = 0
            result += '<sentence category="%s">%s</sentence>' % (res[0],
            res[1])
            cnt += 1
        result += '</page></sentences>'
        nid = request.client_session['nid']
        self.db.lpush('user:%s:recent_queries' % nid, msg)
        self.db.ltrim('user:%s:recent_queries' % nid, 0, MAX_RECENT_QUERIES)
        return Response(result, mimetype='text/xml')

    def _grammar_collect_queries(self, request, type):
        def query_name(type, cnt):
            if cnt == 0:
                return type
            else:
                return type + str(cnt)

        queries = []
        cnt = 0
        key = query_name(type, cnt)
        while key in request.form:
            queries.append(request.form[key])
            cnt += 1
            key = query_name(type, cnt)
        return queries

    def _grammar_save(self, request, gid = None):
        name = request.form['name']
        desc = request.form['desc']
        good_queries = self._grammar_collect_queries(request, 'good-query')
        error_queries = self._grammar_collect_queries(request, 'error-query')
        if not gid:
            gid = self.db.incr('grammar_last_nid')
        if self.db.exists('grammar:%s:good_queries' % gid):
            self.db.delete('grammar:%s:good_queries' % gid)
        for gq in good_queries:
            if len(gq.strip()) > 0:
                self.db.rpush('grammar:%s:good_queries' % gid, gq)
        if self.db.exists('grammar:%s:error_queries' % gid):
            self.db.delete('grammar:%s:error_queries' % gid)
        for eq in error_queries:
            if len(eq.strip()) > 0:
                self.db.rpush('grammar:%s:error_queries' % gid, eq)
        nid = request.client_session['nid']
        self.db.set('grammar:%s:author_nid' % gid, nid)
        t = time.time()
        self.db.set('grammar:%s:create_dt' % gid, t)
        self.db.set('grammar:%s:modify_dt' % gid, t)
        self.db.set('grammar:%s:name' % gid, name)
        self.db.set('grammar:%s:desc' % gid, desc)
        return gid

    @require_auth
    def on_grammar_register(self, request):
        if request.method == 'POST':
            gid = self._grammar_save(request)
            return redirect('/grammar/view/%s' % gid)
        else:
            return self.render_template(request, 'grammar_register.html')

    @require_auth
    def on_grammar_edit(self, request, gid):
        if request.method == 'POST':
            gid = self._grammar_save(request, gid)
            return redirect('/grammar/view/%s' % gid)
        else:
            author_nid = self.db.get('grammar:%d:author_nid' % gid)
            author_email = self.db.get("user:%s:email" % author_nid)
            name = self.db.get('grammar:%d:name' % gid)
            desc = self.db.get('grammar:%d:desc' % gid)
            good_queries = self.db.lrange('grammar:%d:good_queries' % gid, 0,
            -1)
            error_queries = self.db.lrange('grammar:%d:error_queries' % gid,
            0, -1)
            grammar = self._get_grammar(gid)
            return self.render_template(request, 'grammar_register.html',
            edit = True, gr = grammar)

    def _get_grammar(self, gid):
        author_nid = self.db.get('grammar:%d:author_nid' % gid)
        if author_nid:
            author_email = self.db.get("user:%s:email" % author_nid)
            name = self.db.get('grammar:%d:name' % gid)
            desc = self.db.get('grammar:%d:desc' % gid)
            good_queries = self.db.lrange('grammar:%d:good_queries' % gid, 0,
            -1)
            error_queries = self.db.lrange('grammar:%d:error_queries' % gid,
            0, -1)
            return dict(
                nid = gid,
                email = author_email,
                name = name.decode('utf-8'), 
                desc = desc.decode('utf-8'), 
                good_queries = [gq.decode('utf-8') for gq in good_queries],
                error_queries = [eq.decode('utf-8') for eq in error_queries]
                )

    @require_auth
    def on_grammar_view(self, request, gid = None):
        cnt = 0
        if gid:
            grammar = self._get_grammar(gid)
            return self.render_template(request, 'grammar_view.html',
            grammar=grammar)
        else:
            grammars = []
            last_nid = self.db.get('grammar_last_nid')
            if last_nid:
                gid = int(last_nid)
                while gid >= 0:
                    grammar = self._get_grammar(gid)
                    if grammar:
                        grammars.append(grammar)
                    gid -= 1

            return self.render_template(request, 'grammar_view.html',
            grammars=grammars)

    @require_auth
    def on_grammar_delete(self, request, gid):
        self.db.delete('grammar:%s:author_nid' % gid)
        self.db.delete('grammar:%s:create_dt' % gid)
        self.db.delete('grammar:%s:modify_dt' % gid)
        self.db.delete('grammar:%s:name' % gid)
        self.db.delete('grammar:%s:desc' % gid)
        self.db.delete('grammar:%s:good_queries' % gid)
        self.db.delete('grammar:%s:error_queries' % gid)
        return Response('<ok/>', mimetype="text/xml")

    def on_todo(self, request):
        return self.render_template(request, 'todo.html')

    def on_join(self, request):
        data = {}
        for kv in request.data.split('&'):
            key, value = kv.split('=')
            data[key] = urllib.unquote(value)

        if self.db.sismember('all_user_ids', data['id']):
            res = '<fail>%s</fail>' % ('이미 존재하는 ID입니다.');
        else:
            nid = self.db.incr('user_last_nid')
            self.db.hset('user_nid_map', data['id'], nid)
            self.db.set('user:%d:email' % nid, data['id'])
            self.db.set('user:%d:passwd' % nid, data['passwd'])
            self.db.sadd('all_user_ids', data['id'])
            res = '<ok></ok>';
        return Response(res, mimetype='text/xml')

    def on_login(self, request):
        data = {}
        for kv in request.data.split('&'):
            key, value = kv.split('=')
            data[key] = urllib.unquote(value)
        res = '<fail>계정 정보가 맞지 않습니다.</fail>'
        if self.db.sismember('all_user_ids', data['id']):
            nid = self.db.hget('user_nid_map', data['id'])
            if self.db.get('user:%s:passwd' % nid) == data['passwd']:
                request.client_session['login_ts'] = time.time()
                request.client_session['auth_id'] = data['id']
                request.client_session['nid'] = nid
                res = '<ok/>'
        return Response(res, mimetype='text/xml')

    def on_logout(self, request):
        request.client_session.clear();
        return Response("<ok/>", mimetype='text/xml')

    def on_dictionary(self, request, msg):
        dic_req = urllib2.Request(DIC_REQ_URL %
        (urllib.quote(msg.encode('utf-8'))))
        print dic_req
        dic_req.add_header("User-agent", "Mozilla/5.0")
        dic_res = urllib2.urlopen(dic_req)
        dic_data = dic_res.read()
        dic = BeautifulSoup(dic_data).div
        res = dic.find('div', {'class': 'word_num_nobor2'})
        return Response(str(res.dl), mimetype='text/html')

    def on_test(self, request):
        return self.render_template(request, 'test.html')

def create_app(args, with_static=True):
    app = Application(args)
    if with_static:
        app.wsgi_app = SharedDataMiddleware(app.wsgi_app, {
            '/css': os.path.join(os.path.dirname(__file__), 'static/css'),
            '/js': os.path.join(os.path.dirname(__file__), 'static/js'),
            '/img': os.path.join(os.path.dirname(__file__), 'static/img'),
            '/codemirror': os.path.join(os.path.dirname(__file__),
            'static/codemirror'),
        })
    return app


def parse_worker_msg(msg):
    at = msg.find(' ')
    cmd = msg[:at]
    category, show_tag = cmd.split(':')
    show_tag = True if show_tag == 'true' else False
    msg = msg[at + 1:]
    query_msgs = []
    return category, show_tag, msg

def collect_words_and_tags(msg):
    if '$' in msg:
        for sense in senses.keys():
            msg = msg.replace(sense, senses[sense])

    words = []
    tags = []
    parts = []
    # collect part
    m = MSG_LIST_PTRN.search(msg)
    while m:
        ls = m.group(2)
        st, en = m.span()
        pw = msg[:st] 
        if pw:
            parts.append(pw.strip())
        parts.append([w.strip() for w in ls.split(',')])
        msg = msg[en+1:]
        m = MSG_LIST_PTRN.search(msg)
    if msg:
        parts.append(msg) 

    def collect(result_words, result_tags, words):
        for word in words:
            if '/' in word:
                word, tag = word.split('/')
            else:
                tag = ''
            result_words.append(word)
            result_tags.append(tag.upper())

    # iterate
    for part in parts:
        if type(part) == types.ListType:
            wl = []
            tl = []
            collect(wl, tl, part)
            words.append(wl)
            tags.append(tl)
        else:
            collect(words, tags, part.split(' '))
    return words, tags

def worker(idx, args):
    context = zmq.Context()
    work_receiver = context.socket(zmq.PULL)
    work_receiver.connect('tcp://%s:%d' % (args.worker_ip, args.worker_port))
    result_send = context.socket(zmq.PUSH)
    result_send.bind('tcp://%s:%d' % (args.worker_ip, args.result_port + idx))
    while True:
        msg = work_receiver.recv()
        category, show_tag, msg = parse_worker_msg(msg)
        print 'process category (proc #%d): %s' % (idx + 1, category)

        target_words, target_tags = collect_words_and_tags(msg)
        sent_cnt, found_sentences = find_category_sentences(category, show_tag, target_words, target_tags)
        result = []
        result.append(sent_cnt)
        for sentence in found_sentences:
            result.append((category, sentence))
        result_send.send_pyobj(result)

    sys.exit(1)

def init_workers(args):
    for i in range(args.worker_cnt):
        Process(target=worker, args=(i,args)).start()

def init_connection(args):
    print "init zmq connections..",
    global zmq_context, work_sender, result_receiver
    zmq_context = zmq.Context()
    work_sender = zmq_context.socket(zmq.PUSH)
    work_sender.bind('tcp://%s:%d' % (args.worker_ip, args.worker_port))
    result_receiver = zmq_context.socket(zmq.PULL)
    for idx in range(args.worker_cnt):
        result_receiver.connect('tcp://%s:%d' % (args.worker_ip,
        args.result_port + idx))
    print "done"

def start(args):
    #init_logger(args)
    init_corpus()
    init_connection(args)
    init_workers(args)
    from werkzeug.serving import run_simple
    print "Start server with ", args
    if USE_GEVENT:
        WSGIServer((args.server_ip, args.server_port),
        create_app(args)).serve_forever()
    else:
        run_simple(args.server_ip, args.server_port, create_app(args), use_debugger=True,
        use_reloader=False, extra_files=())

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description = 'Enser server')
    subparsers = parser.add_subparsers()

    par_start = subparsers.add_parser('start', help = 'Start server')
    par_start.add_argument('--server_ip', default='0.0.0.0', help="server ip\
    (default: '0.0.0.0')")
    par_start.add_argument('--server_port', type=int, default=5000, help='server port\
    (default: 5000)')
    par_start.add_argument('--worker_cnt', type=int, default=4,
    help='Worker process cnt (default: 4)')
    par_start.add_argument('--worker_ip', default='127.0.0.1',
    help="Worker host ip (default: '127.0.0.1')")
    par_start.add_argument('--worker_port', type=int, default=5557,
    help='Worker message port (default: 5557)')
    par_start.add_argument('--result_port', type=int, default=5560,
    help='Result message port (default: 5560, use + worker_cnt ports)')
    par_start.add_argument('--redis_ip', default='localhost',
    help="Redis host ip (default: 'localhost')")
    par_start.add_argument('--redis_port', type=int, default=6379,
    help='Redis host port (default: 6379)')
    par_start.set_defaults(func = start)

    args = parser.parse_args()
    args.func(args)
