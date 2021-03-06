# -*- test-case-name: vumi.transports.twitter.tests.test_twitter -*-
from twisted.python import log
from twisted.internet.defer import inlineCallbacks
from txtwitter.twitter import TwitterClient
from txtwitter import messagetools

from vumi.transports.base import Transport
from vumi.config import ConfigText, ConfigList


class TwitterTransportConfig(Transport.CONFIG_CLASS):
    screen_name = ConfigText(
        "The screen name for the twitter account",
        required=True, static=True)
    consumer_key = ConfigText(
        "The OAuth consumer key for the twitter account",
        required=True, static=True)
    consumer_secret = ConfigText(
        "The OAuth consumer secret for the twitter account",
        required=True, static=True)
    access_token = ConfigText(
        "The OAuth access token for the twitter account",
        required=True, static=True)
    access_token_secret = ConfigText(
        "The OAuth access token secret for the twitter account",
        required=True, static=True)
    terms = ConfigList(
        "A list of terms to be tracked by the transport",
        default=[], static=True)


class TwitterTransport(Transport):
    """Twitter transport."""

    transport_type = 'twitter'

    CONFIG_CLASS = TwitterTransportConfig
    ENCODING = 'utf8'
    NO_USER_ADDR = 'NO_USER'

    def get_client(self, *a, **kw):
        return TwitterClient(*a, **kw)

    def setup_transport(self):
        config = self.get_static_config()
        self.screen_name = config.screen_name

        self.client = self.get_client(
            config.access_token,
            config.access_token_secret,
            config.consumer_key,
            config.consumer_secret)

        self.track_stream = self.client.stream_filter(
            self.handle_track_stream, track=config.terms)
        self.track_stream.startService()

        self.user_stream = self.client.userstream_user(
            self.handle_user_stream, with_='user')
        self.user_stream.startService()

    @inlineCallbacks
    def teardown_transport(self):
        yield self.user_stream.stopService()
        yield self.track_stream.stopService()

    @inlineCallbacks
    def handle_outbound_message(self, message):
        log.msg("Twitter transport sending %r" % (message,))

        metadata = message['transport_metadata'].get(self.transport_type, {})
        in_reply_to_status_id = metadata.get('status_id')

        try:
            content = message['content']
            if message['to_addr'] != self.NO_USER_ADDR:
                content = "%s %s" % (message['to_addr'], content)

            response = yield self.client.statuses_update(
                content, in_reply_to_status_id=in_reply_to_status_id)

            yield self.publish_ack(
                user_message_id=message['message_id'],
                sent_message_id=response['id_str'])
        except Exception, e:
            yield self.publish_nack(
                user_message_id=message['message_id'],
                sent_message_id=message['message_id'],
                reason='%s' % (e,))

    def is_own_tweet(self, message):
        user = messagetools.tweet_user(message)
        return self.screen_name == messagetools.user_screen_name(user)

    @classmethod
    def screen_name_as_addr(cls, addr):
        return u'@%s' % (addr,)

    @classmethod
    def tweet_to_addr(cls, tweet):
        mentions = messagetools.tweet_user_mentions(tweet)
        to_addr = cls.NO_USER_ADDR

        if mentions:
            mention = mentions[0]
            [start_index, end_index] = mention['indices']

            if start_index == 0:
                to_addr = cls.screen_name_as_addr(mention['screen_name'])

        return to_addr

    @classmethod
    def tweet_from_addr(cls, tweet):
        user = messagetools.tweet_user(tweet)
        return cls.screen_name_as_addr(messagetools.user_screen_name(user))

    @classmethod
    def tweet_content(cls, tweet):
        to_addr = cls.tweet_to_addr(tweet)
        content = messagetools.tweet_text(tweet)

        if to_addr != cls.NO_USER_ADDR and content.startswith(to_addr):
            content = content[len(to_addr):].lstrip()

        return content

    def publish_tweet_message(self, tweet):
        return self.publish_message(
            content=self.tweet_content(tweet),
            to_addr=self.tweet_to_addr(tweet),
            from_addr=self.tweet_from_addr(tweet),
            transport_type=self.transport_type,
            transport_metadata={
                'twitter': {
                    'status_id': messagetools.tweet_id(tweet)
                }
            },
            helper_metadata={
                'twitter': {
                    'in_reply_to_status_id': (
                        messagetools.tweet_in_reply_to_id(tweet)),
                    'in_reply_to_screen_name': (
                        messagetools.tweet_in_reply_to_screen_name(tweet)),
                    'user_mentions': messagetools.tweet_user_mentions(tweet),
                }
            })

    def handle_track_stream(self, message):
        if messagetools.is_tweet(message):
            if self.is_own_tweet(message):
                log.msg("Tracked own tweet: %r" % (message,))
            else:
                log.msg("Tracked a tweet: %r" % (message,))
                self.publish_tweet_message(message)
        else:
            log.msg("Received non-tweet from tracking stream: %r" % message)

    def handle_user_stream(self, message):
        if messagetools.is_tweet(message):
            if self.is_own_tweet(message):
                log.msg("Received own tweet on user stream: %r" % (message,))
            else:
                log.msg("Received tweet on user stream: %r" % (message,))
                self.publish_tweet_message(message)
        else:
            log.msg("Received non-tweet from user stream: %r" % message)
