import unittest

from bridge.mentions import Mention, find_mentions, plan_mentions, replace_mentions, roster_line


class MentionParsingTests(unittest.TestCase):
    def test_finds_full_and_short_handles(self):
        mentions = find_mentions("Reviewed and approved, @agent_two-peer — nice work.")
        self.assertEqual([(mention.raw, mention.localpart, mention.server)
                          for mention in mentions],
                         [("@agent_two-peer", "agent_two-peer", None)])
        full = find_mentions("Ping @agent_one:matrix.example.invalid when done")
        self.assertEqual(full[0].user_id, "@agent_one:matrix.example.invalid")
        self.assertEqual(full[0].server, "matrix.example.invalid")

    def test_trims_trailing_punctuation_and_keeps_repeated_handles(self):
        mentions = find_mentions("@agent_three, @agent_three. and @agent_three please")
        self.assertEqual([mention.raw for mention in mentions],
                         ["@agent_three", "@agent_three", "@agent_three"])
        full = find_mentions("Ping @agent_three:example.test. Then @agent_three:example.test")
        self.assertEqual([mention.raw for mention in full],
                         ["@agent_three:example.test", "@agent_three:example.test"])
        plan = plan_mentions("Ping @agent_three twice: @agent_three", lambda _: "@agent_three:example.test")
        self.assertEqual(plan.user_ids, ["@agent_three:example.test"])
        self.assertEqual(replace_mentions(plan.text, plan, lambda _, user_id: user_id),
                         "Ping @agent_three:example.test twice: @agent_three:example.test")

    def test_ignores_email_addresses_and_code(self):
        self.assertEqual(find_mentions("mail operator@example.com about it"), [])
        self.assertEqual(find_mentions("use `@property` to expose it"), [])
        self.assertEqual(find_mentions("```\n@staticmethod\ndef f():\n```\nreal"), [])
        self.assertEqual([mention.raw for mention in
                          find_mentions("use `@property` then ping @agent_three")],
                         ["@agent_three"])

    def test_plan_splits_known_from_unknown(self):
        roster = {"@agent_two-peer:example.test", "@operator:example.test"}

        def lookup(mention):
            user_id = f"@{mention.localpart}:{mention.server or 'example.test'}"
            return user_id if user_id in roster else None

        plan = plan_mentions("Thanks @agent_two-peer and @ghost.", lookup)
        self.assertEqual(plan.user_ids, ["@agent_two-peer:example.test"])
        self.assertEqual(plan.unknown_raw, ["@ghost"])
        self.assertEqual(replace_mentions(plan.text, plan,
                                          lambda mention, user_id: user_id),
                         "Thanks @agent_two-peer:example.test and @ghost.")

    def test_roster_line_is_bounded(self):
        names = [f"person-{index}" for index in range(30)]
        line = roster_line(names, limit=3)
        self.assertIn("person-0, person-1, person-2", line)
        self.assertIn("and 27 more", line)
        self.assertEqual(roster_line(["b", "a", "b"]), "b, a")
        self.assertEqual(roster_line([]), "")

    def test_plain_mention_helpers_never_mutate_input(self):
        mention = Mention(0, 7, "@alice", "alice", None)
        plan = plan_mentions("@alice hello", lambda item: "@alice:example.test")
        self.assertEqual(plan.text, "@alice hello")
        self.assertEqual(mention.raw, "@alice")
        self.assertIsNone(mention.user_id)


if __name__ == "__main__":
    unittest.main()
