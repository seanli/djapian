import datetime
import os

from django.db import models
from django.utils.itercompat import is_iterable
from djapian.signals import post_save, pre_delete
from django.conf import settings
from django.utils.encoding import smart_unicode

from djapian.resultset import ResultSet, ResultRelatedSet
from djapian import utils, decider

import xapian

class Field(object):
    raw_types = (int, long, float, basestring, bool, models.Model,
                 datetime.time, datetime.date, datetime.datetime)

    def __init__(self, path, weight=utils.DEFAULT_WEIGHT, prefix="", number=None):
        self.path = path
        self.weight = weight
        self.prefix = prefix
        self.number = number

    def get_tag(self):
        return self.prefix.upper()

    def convert(self, field_value, model):
        """
        Generates index values (for sorting) for given field value and its content type
        """
        # If it is a model field make some postprocessing of its value
        try:
            content_type = model._meta.get_field(self.path.split('.', 1)[0])
        except models.FieldDoesNotExist:
            content_type = field_value

        value = field_value

        if isinstance(content_type, (models.IntegerField, int, long)):
            #
            # Integer fields are stored with 12 leading zeros
            #
            value = '%012d' % field_value
        elif isinstance(content_type, (models.BooleanField, bool)):
            #
            # Boolean fields are stored as 't' or 'f'
            #
            if field_value:
                value = 't'
            else:
                value = 'f'
        elif isinstance(content_type, (models.DateTimeField, datetime.datetime)):
            #
            # DateTime fields are stored as %Y%m%d%H%M%S (better
            # sorting)
            #
            value = field_value.strftime('%Y%m%d%H%M%S')
        elif isinstance(content_type, (float, models.FloatField)):
            value = '%.10f' % value

        return value

    def resolve(self, value):
        bits = self.path.split(".")

        for bit in bits:
            try:
                value = getattr(value, bit)
            except AttributeError:
                raise

            if callable(value):
                try:
                    value = value()
                except TypeError:
                    raise

        if isinstance(value, self.raw_types):
            return value
        elif is_iterable(value):
            return ", ".join(value)
        elif isinstance(value, models.Manager):
            return ", ".join(value.all())
        return None

    def extract(self, document):
        if self.number:
            return document.get_value(self.number)

        return None

def paginate(queue, page_size=1000):
    from django.core.paginator import Paginator
    paginator = Paginator(queue, page_size)

    for num in paginator.page_range:
        page = paginator.page(num)

        for obj in page.object_list:
            yield obj

class Indexer(object):
    field_class = Field
    decider = decider.CompositeDecider
    free_values_start_number = 11

    fields = []
    tags = []
    aliases = {}
    trigger = lambda indexer, obj: True
    stemming_lang_accessor = None

    def __init__(self, db, model):
        """
        Initialize an Indexer whose index data to `db`.
        `model` is the Model whose instances will be used as documents.
        Note that fields from other models can still be used in the index,
        but this model will be the one returned from search results.
        """
        self._prepare(db, model)

        #
        # Parse fields
        # For each field checks if it is a tuple or a list and add it's
        # weight
        #
        for field in self.__class__.fields:
            if isinstance(field, (tuple, list)):
                self.fields.append(self.field_class(field[0], field[1]))
            else:
                self.fields.append(self.field_class(field))

        #
        # Parse prefixed fields
        #
        valueno = self.free_values_start_number

        for field in self.__class__.tags:
            tag, path = field[:2]
            if len(field) == 3:
                weight = field[2]
            else:
                weight = utils.DEFAULT_WEIGHT

            self.tags.append(self.field_class(path, weight, prefix=tag, number=valueno))
            valueno += 1

        for tag, aliases in self.__class__.aliases.iteritems():
            if self.has_tag(tag):
                if not isinstance(aliases, (list, tuple)):
                    aliases = (aliases,)
                self.aliases[tag] = aliases
            else:
                raise ValueError("Cannot create alias for tag `%s` that doesn't exist" % tag)

        models.signals.post_save.connect(post_save, sender=self._model)
        models.signals.pre_delete.connect(pre_delete, sender=self._model)

    def __unicode__(self):
        return self.__class__.get_descriptor()
    __str__ = __unicode__

    def has_tag(self, name):
        return self.tag_index(name) is not None

    def tag_index(self, name):
        for field in self.tags:
            if field.prefix == name:
                return field.number

        return None

    @classmethod
    def get_descriptor(cls):
        return ".".join([cls.__module__, cls.__name__]).lower()

    # Public Indexer interface

    def update(self, documents=None, after_index=None, transaction=False, flush=False):
        """
        Update the database with the documents.
        There are some default value and terms in a document:
         * Values:
           1. Used to store the ID of the document
           2. Store the model of the object (in the string format, like
              "project.app.model")
           3. Store the indexer descriptor (module path)
           4..10. Free

         * Terms
           UID: Used to store the ID of the document, so we can replace
                the document by the ID
        """
        # Open Xapian Database
        database = self._db.open(write=True)

        # If doesnt have any document at all
        if documents is None:
            update_queue = self._model.objects.all()
        else:
            update_queue = documents

        counter = [0]

        def flush_each(count=1000):
            """Flushes database every `count` documents"""
            counter[0] += 1

            if counter[0] % count == 0:
                database.flush()

        # make wrappers for transaction management
        if transaction:
            def begin():
                database.begin_transaction(flush=flush)

            def commit():
                database.commit_transaction()

            def cancel():
                database.cancel_transaction()
        else:
            begin = commit = cancel = lambda: None

        # Get each document received
        for obj in paginate(update_queue):
            begin()
            try:
                if not self.trigger(obj):
                    self.delete(obj.pk, database)
                    commit()
                    continue

                doc = xapian.Document()
                #
                # Add default terms and values
                #
                uid = self._create_uid(obj)
                doc.add_term(self._create_uid(obj))
                self._insert_meta_values(doc, obj)

                generator = xapian.TermGenerator()
                generator.set_database(database)
                generator.set_document(doc)
                generator.set_flags(xapian.TermGenerator.FLAG_SPELLING)

                stem_lang = self._get_stem_language(obj)
                if stem_lang:
                    generator.set_stemmer(xapian.Stem(stem_lang))

                for field in self.fields + self.tags:
                    # Trying to resolve field value or skip it
                    try:
                        value = field.resolve(obj)
                    except AttributeError:
                        continue

                    if field.prefix:
                        index_value = field.convert(value, self._model)
                        if index_value is not None:
                            doc.add_value(field.number, smart_unicode(index_value))

                    prefix = smart_unicode(field.get_tag())
                    generator.index_text(smart_unicode(value), field.weight, prefix)
                    if prefix:  # if prefixed then also index without prefix
                        generator.index_text(smart_unicode(value), field.weight)

                database.replace_document(uid, doc)
                #FIXME: ^ may raise InvalidArgumentError when word in
                #         text larger than 255 simbols
                if after_index:
                    after_index(obj)

                commit()
            except:
                cancel()

            if transaction:
                if not flush:
                    flush_each()
            else:
                if flush:
                    database.flush()
                else:
                    flush_each()

        database.flush()

    def search(self, query):
        return ResultSet(self, query)
 
    def related(self, hits):
        return ResultRelatedSet(self, hits)


    def delete(self, obj, database=None):
        """
        Delete a document from index
        """
        try:
            if database is None:
                database = self._db.open(write=True)
            database.delete_document(self._create_uid(obj))
        except (IOError, RuntimeError, xapian.DocNotFoundError), e:
            pass

    def document_count(self):
        return self._db.document_count()

    __len__ = document_count

    def clear(self):
        self._db.clear()

    # Private Indexer interface
    def _prepare(self, db, model=None):
        """Initialize attributes"""
        self._db = db
        self._model = model
        self._model_name = model and utils.model_name(model)

        self.fields = [] # Simple text fields
        self.tags = [] # Prefixed fields
        self.aliases = {}

    def _get_meta_values(self, obj):
        if isinstance(obj, models.Model):
            pk = obj.pk
        else:
            pk = obj
        return [pk, self._model_name, self.__class__.get_descriptor()]

    def _insert_meta_values(self, doc, obj, start=1):
        for value in self._get_meta_values(obj):
            doc.add_value(start, smart_unicode(value))
            start += 1
        return start

                
    def _create_uid(self, obj):
        """
        Generates document UID for given object
        """
        return "UID-" + "-".join(map(smart_unicode, self._get_meta_values(obj)))




    def _do_related(self, matches):
       """
       Fetches documents related to original set searched  for
       """
       database = self._db.open()
       enquire = xapian.Enquire(database)
       rdocs = xapian.RSet()
       count = len(matches)
       if count < 10:
           count = 10
       if  count > 40:
           count = 40
       for match in matches:
           rdocs.add_document(match.get_docid())
       terms = enquire.get_eset(count, rdocs)
       qterms = set([term.term for term in terms])
       query = []
       #print qterms
       for term in qterms:
           if term.islower():
               query.append(term)
           else:
               for tag in self.tags:
                   tag = tag.get_tag()
                   if term.startswith(tag):
                       term = term[len(tag):]
                       query.append(term)
               
       query = set(query)
       print query
       return ' OR '.join(query)
                           

    def _do_search(self, query, offset, limit, order_by, flags, stemming_lang,
                    filter, exclude):
        """
        flags are as defined in the Xapian API :
        http://www.xapian.org/docs/apidoc/html/classXapian_1_1QueryParser.html
        Combine multiple values with bitwise-or (|).
        """
        database = self._db.open()
        enquire = xapian.Enquire(database)

        if order_by in (None, 'RELEVANCE'):
            enquire.set_sort_by_relevance()
        else:
            ascending = True
            if order_by.startswith('-'):
                ascending = False

            if order_by[0] in '+-':
                order_by = order_by[1:]

            try:
                valueno = self.tag_index(order_by)
            except (ValueError, TypeError):
                raise ValueError("Field %s cannot be used in order_by clause"
                                 " because it doen't exist in index" % order_by)

            enquire.set_sort_by_relevance_then_value(valueno, ascending)

        query, query_parser = self._parse_query(query, database, flags, stemming_lang)
        enquire.set_query(
            query
        )

        decider = self.decider(self._model, self.tags, filter, exclude)

        return enquire.get_mset(
            offset,
            limit,
            None,
            decider
        ), query, query_parser
      
  


    def _get_stem_language(self, obj=None):
        """
        Returns stemmig language for given object if acceptable or model wise
        """
        language = getattr(settings, "DJAPIAN_STEMMING_LANG", "none") # Use the language defined in DJAPIAN_STEMMING_LANG

        if language == "multi":
            if obj:
                try:
                    language = self.field_class(self.stemming_lang_accessor).resolve(obj)
                except AttributeError:
                    pass
            else:
                language = "none"

        return language

    def _parse_query(self, term, db, flags, stemming_lang):
        """
        Parses search queries
        """
        # Instance Xapian Query Parser
        query_parser = xapian.QueryParser()

        for field in self.tags:
            query_parser.add_prefix(field.prefix.lower(), field.get_tag())
            if field.prefix in self.aliases:
                for alias in self.aliases[field.prefix]:
                    query_parser.add_prefix(alias, field.get_tag())

        query_parser.set_database(db)
        query_parser.set_default_op(xapian.Query.OP_AND)

        if stemming_lang in (None, "none"):
            stemming_lang = self._get_stem_language()

        if stemming_lang:
            query_parser.set_stemmer(xapian.Stem(stemming_lang))
            query_parser.set_stemming_strategy(xapian.QueryParser.STEM_SOME)

        parsed_query = query_parser.parse_query(term, flags)

        return parsed_query, query_parser

class CompositeIndexer(Indexer):
    def __init__(self, *indexers):
        from djapian.database import CompositeDatabase

        self._prepare(
            db=CompositeDatabase([indexer._db for indexer in indexers])
        )

    def clear(self):
        raise NotImplementedError

    def update(self, *args):
        raise NotImplementedError
