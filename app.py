# -*- coding: utf-8 -*-
#
# This file is mostly taken from the Tool Labs Flask + OAuth WSGI tutorial
# https://wikitech.wikimedia.org/wiki/Help:Tool_Labs/My_first_Flask_OAuth_tool
#
# Copyright (C) 2017 Bryan Davis and contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free
# Software Foundation, either version 3 of the License, or (at your
# option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# for
# more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.

import flask
import os
import yaml
import mwoauth
import requests
import json
import md5
import codecs
import datetime
from requests_oauthlib import OAuth1
import mwparserfromhell
import main
from difflib import HtmlDiff

app = flask.Flask(__name__)

__dir__ = os.path.dirname(__file__)
app.config.update(
    yaml.safe_load(open(os.path.join(__dir__, 'config.yaml'))))

class InvalidUsage(Exception):
    status_code = 400

    def __init__(self, message, status_code=None):
        Exception.__init__(self)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        self.payload = payload

@app.errorhandler(InvalidUsage)
def handle_invalid_usage(error):
    response = flask.render_template("error.html", message=error.message)
    response.status_code = error.status_code
    return response

@app.errorhandler(Exception)
def handle_invalid_usage(error):
    import traceback
    tb = traceback.format_exc().replace('\n','<br/>\n')
    response = flask.render_template("error.html", message=
        str(type(error))+' '+str(error)+'\n<br/>\n'+tb)
    return response


@app.route('/')
def index():
    context = {
        'username' : flask.session.get('username', None),
        'success' : flask.request.args.get('success'),
    }
    return flask.render_template("index.html", **context)

def edit_wiki_page(page_name, content, summary=None):
    access_token = flask.session.get('access_token', None)
    auth = OAuth1(
		app.config['CONSUMER_KEY'],
		app.config['CONSUMER_SECRET'],
		access_token['key'],
		access_token['secret'])

    # Get token
    r = requests.get('https://en.wikipedia.org/w/api.php', params={
	'action':'query',
	'meta':'tokens',
        'format': 'json',
    }, auth=auth)
    r.raise_for_status()
    token = r.json()['query']['tokens']['csrftoken']
    
    r = requests.post('https://en.wikipedia.org/w/api.php', data={
	'action':'edit',
        'title': page_name,
	'text': content,
        'summary': summary,
        'format': 'json',
        'token': token,
    }, auth=auth)
    r.raise_for_status()
	
def cached(fun, force, key, *args, **kwargs):
    r = md5.md5()
    r.update(key.encode('utf-8'))
    h = r.hexdigest()
    cache_fname = 'cache/%s.html' % h
    if not force and os.path.isfile(cache_fname):
	with codecs.open(cache_fname, 'r', 'utf-8') as f:
	    val = f.read()
	return val
    else:
	value = fun(*args, **kwargs)
	with codecs.open(cache_fname, 'w', 'utf-8') as f:
	    f.write(value)
	return value
    
@app.route('/process')
def process():
    page_name = flask.request.args.get('name')
    force = flask.request.args.get('refresh') == 'true'
    text = main.get_page_over_api(page_name)
    all_templates = main.add_oa_links_in_references(text)
    filtered = filter(lambda e: e.proposed_change, all_templates)
    context = {
	'proposed_edits': filtered,
	'page_name' : page_name,
        'utcnow': datetime.datetime.utcnow(),
    } 
    return cached(flask.render_template, force, page_name, "change.html", **context)
    #return tpl

def make_new_wikicode(text, form_data):
    wikicode = mwparserfromhell.parse(text)
    change_made = False
    for template in wikicode.filter_templates():
        edit = main.TemplateEdit(template)
        if edit.classification == 'ignored':
            continue
        proposed_addition = form_data.get(edit.orig_hash)
        if proposed_addition:
            try:
                edit.update_template(proposed_addition)
                change_made = True
            except ValueError:
                pass # TODO report to the user
    return unicode(wikicode), change_made


@app.route('/perform-edit', methods=['POST'])
def perform_edit():
    data = flask.request.form

    # Check we are logged in
    access_token =flask.session.get('access_token', None)
    if not access_token:
        return flask.redirect(flask.url_for('login'))

    page_name = data.get('name')
    if not page_name:
        raise InvalidUsage('Page title is required')
    summary = data.get('summary')
    if not summary:
        raise InvalidUsage('No summary provided')
        
    # Get the page
    text = main.get_page_over_api(page_name)
    
    # Perform each edit
    new_text, change_made = make_new_wikicode(text, data)

    # Save the page
    if change_made:
        edit_wiki_page(page_name, new_text, summary)
        return flask.redirect(flask.url_for('index', success='true'))
    else:
        return flask.redirect(flask.url_for('index', success='nothing'))


def make_diff(old, new):
    """
    Render in HTML the diff between two texts
    """
    df = HtmlDiff()
    old_lines = old.splitlines(1)
    new_lines = new.splitlines(1)
    html = df.make_table(old_lines, new_lines, context=True)
    html = html.replace(' nowrap="nowrap"','')
    return html

@app.route('/preview-edit', methods=['POST'])
def preview_edit():
    data = flask.request.form

    page_name = data.get('name')
    if not page_name:
        raise InvalidUsage('Page title is required')
    summary = data.get('summary')
    if not summary:
        raise InvalidUsage('No summary provided')
        
    # Get the page
    text = main.get_page_over_api(page_name)
    
    # Perform each edit
    new_text, change_made = make_new_wikicode(text, data)

    diff = make_diff(text, new_text)
    return '<div class="diffcontainer">'+diff+'</div>'
    

@app.route('/login')
def login():
    """Initiate an OAuth login.

    Call the MediaWiki server to get request secrets and then redirect
the
    user to the MediaWiki server to sign the request.
    """
    consumer_token = mwoauth.ConsumerToken(
        app.config['CONSUMER_KEY'], app.config['CONSUMER_SECRET'])
    try:
        redirect, request_token = mwoauth.initiate(
            app.config['OAUTH_MWURI'], consumer_token)
    except Exception:
        app.logger.exception('mwoauth.initiate failed')
        return flask.redirect(flask.url_for('index'))
    else:
        flask.session['request_token'] = dict(zip(
            request_token._fields, request_token))
        return flask.redirect(redirect)


@app.route('/oauth-callback')
def oauth_callback():
    """OAuth handshake callback."""
    if 'request_token' not in flask.session:
        flask.flash(u'OAuth callback failed. Are cookies disabled?')
        return flask.redirect(flask.url_for('index'))

    consumer_token = mwoauth.ConsumerToken(
        app.config['CONSUMER_KEY'], app.config['CONSUMER_SECRET'])

    try:
        access_token = mwoauth.complete(
            app.config['OAUTH_MWURI'],
            consumer_token,
            mwoauth.RequestToken(**flask.session['request_token']),
            flask.request.query_string)

        identity = mwoauth.identify(
            app.config['OAUTH_MWURI'], consumer_token, access_token)
    except Exception as e:
        app.logger.exception('OAuth authentication failed')

    else:
        flask.session['access_token'] = dict(zip(
            access_token._fields, access_token))
        flask.session['username'] = identity['username']

    return flask.redirect(flask.url_for('index'))


@app.route('/logout')
def logout():
    """Log the user out by clearing their session."""
    flask.session.clear()
    return flask.redirect(flask.url_for('index'))

@app.route('/css/<path:path>')
def send_css(path):
    try:
	    return flask.send_from_directory('css', path)
    except Exception as e:
	with open('exception', 'w') as f:
	    f.write(str(type(e))+' '+str(e))

@app.route('/edits/<path:path>')
def send_edits(path):
    return flask.send_from_directory('edits', path)

if __name__ == "__main__":
    app.run(host='0.0.0.0')

