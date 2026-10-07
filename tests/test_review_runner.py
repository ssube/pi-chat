import os
import unittest
from unittest.mock import Mock, patch
from tools.review_pr import ReviewError, parse_review, publish_review


class ReviewRunnerTests(unittest.TestCase):
    def test_valid_verdict_and_credential_rejection(self):
        self.assertEqual(parse_review('VERDICT: APPROVE\nChecked the diff.'), ('APPROVED', 'Checked the diff.'))
        with self.assertRaises(ReviewError):
            parse_review('Looks fine.')
        with patch.dict(os.environ, {'TEST_TOKEN': 'synthetic-secret-value'}):
            with self.assertRaises(ReviewError):
                parse_review('VERDICT: COMMENT\nsynthetic-secret-value')

    def test_head_change_prevents_publication(self):
        api = Mock()
        api.request.return_value = {'state': 'open', 'head': {'sha': 'b' * 40}}
        with self.assertRaisesRegex(ReviewError, 'changed'):
            publish_review(api, 1, 'a' * 40, 'VERDICT: COMMENT\nA finding.')
        api.request.assert_called_once_with('GET', '/pulls/1')

    def test_pending_review_is_submitted_and_verified(self):
        api = Mock()
        api.request.side_effect = [dict(state='open', head=dict(sha='a' * 40)),
                                   dict(state='PENDING', id=9), dict(state='APPROVED', id=9)]
        result = publish_review(api, 1, 'a' * 40, 'VERDICT: APPROVE\nChecked the diff.')
        self.assertEqual(result['verdict'], 'APPROVED')
        self.assertEqual(api.request.call_count, 3)
