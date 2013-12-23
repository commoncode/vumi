from twisted.internet.defer import inlineCallbacks
from twisted.internet import defer
from twisted.web import error

from vumi.message import TransportUserMessage
from vumi.tests.helpers import VumiTestCase
from vumi.transports.twitter import TwitterTransport
from vumi.transports.tests.helpers import TransportHelper


class Thing(object):
    """This is what ruby taught me"""
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __getattr__(self, attr):
        return self.kwargs[attr]


class FakeTwitter(object):
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.raise_update_error = False

    def update(self, content):
        if self.raise_update_error:
            return defer.fail(error.Error(503, 'Fail Whale'))
        else:
            return defer.succeed('post-id')

    def track(self, delegate, terms):
        self.track_delegate = delegate
        self.track_terms = terms

    def replies(self, delegate, params={}, extra_args=None):
        self.replies_delegate = delegate
        self.replies_params = params
        self.replies_extra_args = extra_args

    def send_fake_replies(self, *replies):
        for reply in replies:
            self.replies_delegate(reply)

    def send_fake_track(self, *tracks):
        for status in tracks:
            self.track_delegate(status)


class TestTwitterTransport(VumiTestCase):

    @inlineCallbacks
    def setUp(self):
        config = {
            'app_name': 'testapp',
            'consumer_key': 'consumer1',
            'consumer_secret': 'consumersecret1',
            'access_token': 'token1',
            'access_token_secret': 'tokensecret1',
            'terms': ['some', 'trending', 'topic'],
            'allow_post': True,
        }
        self.tx_helper = self.add_helper(TransportHelper(TwitterTransport))
        self.transport = yield self.tx_helper.get_transport(
            config, start=False)
        self.transport._twitter_class = FakeTwitter
        self.transport._twitter_post_class = FakeTwitter
        yield self.transport.startWorker()

    @inlineCallbacks
    def test_handle_replies(self):
        reply = Thing(
            id=1,
            published=1,
            text='@tweeter hi there',
            title='tweeter',
            author=Thing(screen_name='replier'),
            raw={
                'some': 'raw',
                'fields': 'and values',
            }
        )
        self.transport.twitter.send_fake_replies(reply)
        [msg] = yield self.tx_helper.wait_for_dispatched_inbound(1)
        self.assertEqual(msg['from_addr'], 'replier')
        self.assertEqual(msg['to_addr'], 'tweeter')
        self.assertEqual(msg['content'], '@tweeter hi there')
        self.assertEqual(msg['session_event'],
                         TransportUserMessage.SESSION_RESUME)
        self.assertEqual(msg['message_id'], 1)
        last_reply_timestamp = yield self.transport.redis.get(
            'last_reply_timestamp')
        self.assertEqual(last_reply_timestamp, '1')

    @inlineCallbacks
    def test_handle_track(self):
        status = Thing(
            id=1,
            text='text',
            in_reply_to_screen_name='@reply_to',
            user=Thing(screen_name='@screen_name'),
            raw={
                'some': 'raw',
                'fields': 'and values',
            }
        )
        self.transport.twitter.send_fake_track(status)
        [msg] = yield self.tx_helper.wait_for_dispatched_inbound(1)
        self.assertEqual(msg['from_addr'], '@screen_name')
        self.assertEqual(msg['to_addr'], '@reply_to')
        self.assertEqual(msg['content'], 'text')
        self.assertEqual(msg['message_id'], '1')
        self.assertEqual(msg['session_event'],
                         TransportUserMessage.SESSION_NONE)

    @inlineCallbacks
    def test_outbound_disabled(self):
        self.transport.allow_post = False
        msg = yield self.tx_helper.make_dispatch_outbound("outbound")
        [nack] = yield self.tx_helper.wait_for_dispatched_events(1)
        self.assertEqual(nack['user_message_id'], msg['message_id'])
        self.assertEqual(nack['sent_message_id'], msg['message_id'])
        self.assertEqual(nack['nack_reason'],
            'Posting to twitter is disabled.')

    @inlineCallbacks
    def test_outbound(self):
        msg = yield self.tx_helper.make_dispatch_outbound("outbound")
        [ack] = yield self.tx_helper.wait_for_dispatched_events(1)
        self.assertEqual(ack['user_message_id'], msg['message_id'])
        self.assertEqual(ack['sent_message_id'], 'post-id')

    @inlineCallbacks
    def test_outbound_fail_whale(self):
        self.transport.twitter_post.raise_update_error = True
        msg = yield self.tx_helper.make_dispatch_outbound("outbound")
        [nack] = yield self.tx_helper.wait_for_dispatched_events(1)
        self.assertEqual(nack['user_message_id'], msg['message_id'])
        self.assertEqual(nack['sent_message_id'], msg['message_id'])
        self.assertEqual(nack['nack_reason'],
            '503 Fail Whale')
