# -*- test-case-name: vumi.transports.twitter.tests.test_twitter -*-

import json

from twisted.python import log
from twisted.internet.defer import inlineCallbacks
from twisted.internet import task
from twisted.web import error
from twittytwister import twitter
from oauth import oauth

from vumi.transports.base import Transport
from vumi.message import TransportUserMessage
from vumi.persist.txredis_manager import TxRedisManager


def _parse_update_response(response_body):
    return json.loads(response_body)['id_str']


class FragileUpdatePatchedTwitter(twitter.Twitter):
    """
    This is a fragile hack using twitter.Twitter to post status updates using
    the v1.1 REST API. It has been manually tested and works as of 2013-12-23,
    but we really should be replacing this horrible client library with a
    different one instead of balancing additional hacks on it.
    """

    def update(self, status, source=None, params={}):
        # XXX: This assumes the private internals of Twitter.twitter do what we
        #      expect.
        params = params.copy()
        params['status'] = status
        if source:
            params['source'] = source
        d = self._Twitter__post('/statuses/update.json', params)
        return d.addCallback(_parse_update_response)


class TwitterTransport(Transport):
    """Twitter transport."""

    transport_type = 'twitter'

    _twitter_class = twitter.TwitterFeed
    _twitter_post_class = FragileUpdatePatchedTwitter

    def validate_config(self):
        self.consumer_key = self.config['consumer_key']
        self.consumer_secret = self.config['consumer_secret']
        self.access_token = self.config['access_token']
        self.access_token_secret = self.config['access_token_secret']
        self.r_config = self.config.get('redis_manager', {})
        self.r_prefix = "%(transport_name)s@%(app_name)s:replies" % self.config
        self.terms = set(self.config.get('terms'))
        self.check_replies_interval = int(self.config.get(
                            'check_replies_interval', 60))
        self.allow_post = self.config.get('allow_post', False)

    @inlineCallbacks
    def setup_transport(self):
        redis = yield TxRedisManager.from_config(self.r_config)
        self.redis = redis.sub_manager(self.r_prefix)
        consumer = oauth.OAuthConsumer(self.consumer_key, self.consumer_secret)
        token = oauth.OAuthToken(self.access_token, self.access_token_secret)
        self.twitter = self._twitter_class(consumer=consumer, token=token)
        yield self.start_tracking_terms()
        if self.check_replies_interval > 0:
            self.start_checking_for_replies()
        if self.allow_post:
            self.twitter_post = self._twitter_post_class(
                consumer=consumer, token=token,
                base_url="https://api.twitter.com/1.1")

    @inlineCallbacks
    def start_tracking_terms(self):
        if self.terms:
            self.stream = yield self.twitter.track(self.handle_track,
                                                   self.terms)

    def start_checking_for_replies(self):
        self.check_replies = task.LoopingCall(self.check_for_replies)
        self.check_replies.start(self.check_replies_interval)

    def teardown_transport(self):
        # Stop our reply checking, if any.
        if hasattr(self, 'check_replies') and self.check_replies.running:
            self.check_replies.stop()

        # Stop our filter stream, if any.
        if hasattr(self, 'stream'):
            if getattr(self.stream, 'transport', None) is not None:
                self.stream.transport.stopProducing()

        return self.redis._close()

    def check_for_replies(self):
        return self.twitter.replies(self.handle_replies)

    @inlineCallbacks
    def handle_outbound_message(self, message):
        """
        TODO:   Add in_reply_to_status_id parameter if present,
                need access to the Twitter docs to do so at the
                moment.
        """
        log.msg("Twitter transport sending %r" % (message,))
        if not self.allow_post:
            yield self.publish_nack(user_message_id=message['message_id'],
                                    sent_message_id=message['message_id'],
                                    reason="Posting to twitter is disabled.")
            return
        try:
            post_id = yield self.twitter_post.update(message['content'])
            yield self.publish_ack(user_message_id=message['message_id'],
                                sent_message_id=post_id)
        except error.Error, e:
            yield self.publish_nack(user_message_id=message['message_id'],
                                sent_message_id=message['message_id'],
                                reason=str(e))

    @inlineCallbacks
    def handle_replies(self, message):
        """
        handle_replies is called at a regular interval to check for replies
        that are received on the given account. Attached the SESSION_RESUME
        event type to the messages to keep them distinguishable from messages
        arriving by tracking terms in realtime.
        """
        last_reply_timestamp = yield self.redis.get('last_reply_timestamp')
        if (last_reply_timestamp is None or
                message.published > last_reply_timestamp):
            self.publish_message(
                message_id=message.id,
                content=message.text,
                to_addr=message.title,
                from_addr=message.author.screen_name,
                session_event=TransportUserMessage.SESSION_RESUME,
                transport_type=self.transport_type,
                transport_metadata=message.raw,
            )
            yield self.redis.set('last_reply_timestamp', message.published)

    def handle_track(self, status):
        """
        Get hits with a status update whenever a tweet matching
        a term being tracked is detected. Attached the SESSION_NONE
        event type as these messages aren't necessarily part of a
        conversation.
        """
        self.publish_message(
            message_id=unicode(status.id),
            content=status.text,
            to_addr=status.in_reply_to_screen_name or '',
            from_addr=status.user.screen_name,
            session_event=TransportUserMessage.SESSION_NONE,
            transport_type=self.transport_type,
            transport_metadata=status.raw,
        )
