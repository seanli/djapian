import os
from datetime import datetime, timedelta

from django.db import models
from django.test import TestCase

import djapian
from djapian import Indexer, Field

class Person(models.Model):
    name = models.CharField(max_length=150)

    def __unicode__(self):
        return self.name

    class Meta:
        app_label = "djapian"

class Entry(models.Model):
    author = models.ForeignKey(Person, related_name="entries")
    title = models.CharField(max_length=250)
    tags = models.CharField(max_length=250, null=True)
    created_on = models.DateTimeField(default=datetime.now)
    rating = models.FloatField(default=0)

    asset_count = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    text = models.TextField()

    editors = models.ManyToManyField(Person, related_name="edited_entries")

    def headline(self):
        return "%s - %s" % (self.author, self.title)

    def __unicode__(self):
        return self.title

    class Meta:
        app_label = "djapian"

class Comment(models.Model):
    entry = models.ForeignKey(Entry)

    author = models.ForeignKey(Person)
    text = models.TextField()

    class Meta:
        app_label = "djapian"

class EntryIndexer(Indexer):
    fields = ["text"]
    tags = [
        ("author", "author.name"),
        ("title", "title", 3),
        ("tag", "tags", 2),
        ("date", "created_on"),
        ("active", "is_active"),
        ("count", "asset_count"),
        ("editors", "editors"),
        ('rating', 'rating'),
    ]
    aliases = {
        "title": "subject",
        "author": "user",
    }
    trigger = lambda indexer, obj: obj.is_active

class CommentIndexer(Indexer):
    fields = ['text']
    tags = [
        ('author', 'author.name')
    ]

djapian.add_index(Entry, EntryIndexer, attach_as='indexer')
djapian.add_index(Comment, CommentIndexer, attach_as='indexer')

class BaseTestCase(TestCase):
    def tearDown(self):
        Entry.indexer.clear()

class BaseIndexerTest(object):
    def setUp(self):
        self.person = Person.objects.create(name="Alex")

        self.entries= [
            Entry.objects.create(
                author=self.person,
                title="Test entry",
                rating=4.5,
                text="Not large text field wich helps us to test Djapian"
            ),
            Entry.objects.create(
                author=self.person,
                title="Another test entry - second",
                rating=3.6,
                text="Another not useful text message for tests",
                asset_count=5,
                created_on=datetime.now()-timedelta(hours=4)
            ),
            Entry.objects.create(
                author=self.person,
                title="Third entry for testing",
                rating=4.65,
                text="Third message text",
                asset_count=7,
                created_on=datetime.now()-timedelta(hours=2)
            ),
            Entry.objects.create(
                author=self.person,
                title="Innactive entry",
                is_active=False,
                text="Text wich will not be indexed"
            )
        ]

        Entry.indexer.update()

        self.comments =[
            Comment.objects.create(
                entry=self.entries[0],
                author=self.person,
                text='Hey, I comment my own entry!'
            )
        ]

        Comment.indexer.update()
