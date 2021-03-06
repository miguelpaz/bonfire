import logging
import math
import time
from elasticsearch import Elasticsearch
from elasticsearch.exceptions import (
    NotFoundError,
    TransportError,
    ConflictError)
from elasticsearch.helpers import bulk
from .config import get_elasticsearch_hosts
from .dates import now, get_since_now, get_query_dates

def logger():
    return  logging.getLogger(__name__)

RESULTS_CACHE_INDEX = 'bonfire_results_cache'
RESULTS_CACHE_DOCUMENT_TYPE = 'results'
URL_CACHE_INDEX = 'bonfire_url_cache'
CACHED_URL_DOCUMENT_TYPE = 'url'
TOP_CONTENT_INDEX = 'bonfire_top_content'
TOP_CONTENT_DOCUMENT_TYPE = 'top_content'
USER_DOCUMENT_TYPE = 'user'
CONTENT_DOCUMENT_TYPE = 'content'
TWEET_DOCUMENT_TYPE = 'tweet'
UNPROCESSED_TWEET_DOCUMENT_TYPE = 'rawtweet'
from .mappings import (
    RESULTS_CACHE_MAPPING,
    CACHED_URL_MAPPING,
    TOP_CONTENT_MAPPING,
    USER_MAPPING,
    CONTENT_MAPPING,
    TWEET_MAPPING,
    UNPROCESSED_TWEET_MAPPING)


_es_connections = {}
from .elastic import ESClient
def es(universe):
    """Return new-style Elasticsearch client connection for the universe"""
    global _es_connections
    if not universe in _es_connections:
        _es_connections[universe] = ESClient(
            hosts=get_elasticsearch_hosts(universe))
    return _es_connections[universe]



def build_universe_mappings(universe, rebuild=False):
    """Create and map the universe."""
    # TODO: Can we rebuild without losing our data by using aliases and
    # re-indexing? See here:
    # http://www.elasticsearch.org/blog/changing-mapping-with-zero-downtime/
    # Keys are the index names. 
    # Values are key/value pairs of the doc types and doc mappings.
    all_indices = {
        universe: {
            USER_DOCUMENT_TYPE: USER_MAPPING,
            CONTENT_DOCUMENT_TYPE: CONTENT_MAPPING,
            TWEET_DOCUMENT_TYPE: TWEET_MAPPING,
            UNPROCESSED_TWEET_DOCUMENT_TYPE: UNPROCESSED_TWEET_MAPPING
        },
        URL_CACHE_INDEX: {
            CACHED_URL_DOCUMENT_TYPE: CACHED_URL_MAPPING
        },
        RESULTS_CACHE_INDEX: {
            RESULTS_CACHE_DOCUMENT_TYPE: RESULTS_CACHE_MAPPING
        },
        TOP_CONTENT_INDEX: {
            TOP_CONTENT_DOCUMENT_TYPE: TOP_CONTENT_MAPPING
        }
    }
    for index_name, index_mapping in all_indices.items():
        if not es(universe).indices.exists(index_name):
            es(universe).indices.create(index=index_name)
        for doc_type, doc_mapping in index_mapping.items():
            if rebuild:
                try:
                    es(universe).indices.delete_mapping(
                        index=index_name, doc_type=doc_type) 
                except NotFoundError:
                    pass
            es(universe).indices.put_mapping(
                doc_type, doc_mapping, index=index_name)


def get_all_docs(universe, index, doc_type, body={}, size=None, field='_id'):
    """
    Helper function to return all values in a certain field.
    Defaults to retrieving all ids from a given index and doc type.

    :arg universe: current universe.
    :arg index: current index.
    :arg doc_type: the type of doc to return all values for.
    :arg body: add custom body, or leave blank to retrieve everything.
    :arg size: limit by size, or leave as None to retrieve all.
    :arg field: retrieve all of a specific field. Defaults to id.
    """
    chunk_size, start = 5000, 0
    all_results = []
    while True:
        if field == '_id':
            res = es(universe).search(index=universe, doc_type=doc_type,
                body=body, size=chunk_size, from_=start,
                _source=False)
            all_results.extend([u._id for u in res])
        else:
            res = es(universe).search(index=universe, doc_type=doc_type,
                body=body, size=chunk_size, from_=start,
                _source_include=[field])
            all_results.extend([u[field] for u in res])
        if size is None:
            size = res.total_hits
        start += chunk_size
        if start >= size:
            break
    return all_results


def cleanup(universe, days=30):
    """Delete everything in the universe that is more than days old.
    Does not apply to top content."""
    client = es(universe)
    actions = []

    body = {
        'filter': {
            'range': {
                'created': {
                    'lt': 'now-%dd' % days
                }
            }
        }
    }

    # Delete all tweets that are over days old
    old_tweet_ids = get_all_docs(universe,
        index=universe,
        doc_type=TWEET_DOCUMENT_TYPE,
        body=body)
    for tweet_id in old_tweet_ids:
        actions.append({
            '_op_type': 'delete',
            '_index': universe,
            '_type': TWEET_DOCUMENT_TYPE,
            '_id': tweet_id,
        })

    # Delete old cached results and urls
    body['filter']['range']['cached_at'] = body['filter']['range'].pop('created')
    old_results_ids = get_all_docs(universe,
        index=RESULTS_CACHE_INDEX,
        doc_type=RESULTS_CACHE_DOCUMENT_TYPE,
        body=body)
    for result_id in old_results_ids:
        actions.append({
            '_op_type': 'delete',
            '_index': RESULTS_CACHE_INDEX,
            '_type': RESULTS_CACHE_DOCUMENT_TYPE,
            '_id': result_id
        })
    old_urls_ids = get_all_docs(universe,
        index=URL_CACHE_INDEX,
        doc_type=CACHED_URL_DOCUMENT_TYPE,
        body=body)
    for url in old_urls_ids:
        actions.append({
            '_op_type': 'delete',
            '_index': URL_CACHE_INDEX,
            '_type': CACHED_URL_DOCUMENT_TYPE,
            '_id': url
        })

    # This actually deletes everything
    bulk(client, actions)

    # Now we can quickly get all content that doesn't have a tweet
    all_urls = set(get_all_docs(universe, 
        index=universe, 
        doc_type=CONTENT_DOCUMENT_TYPE))
    tweeted_urls = set(get_all_docs(universe,
        index=universe,
        doc_type=TWEET_DOCUMENT_TYPE,
        field='content_url'))
    obsolete_urls = all_urls - tweeted_urls

    # Delete those too
    actions = []
    for url in obsolete_urls:
        actions.append({
            '_op_type': 'delete',
            '_index': universe,
            '_type': CONTENT_DOCUMENT_TYPE,
            '_id': url
            })
    bulk(client, actions)
    

def get_cached_url(universe, url):
    """Get a resolved URL from the index.
    Returns None if URL doesn't exist."""
    try:
        return es(universe).get_source(index=URL_CACHE_INDEX, 
            id=url.rstrip('/'), doc_type=CACHED_URL_DOCUMENT_TYPE)['resolved']
    except NotFoundError:
        return None


def set_cached_url(universe, url, resolved_url):
    """Index a URL and its resolution in Elasticsearch"""
    body = {
        'url': url.rstrip('/'),
        'resolved': resolved_url.rstrip('/'),
        'cached_at': now(stringify=True)
    }
    es(universe).index(index=URL_CACHE_INDEX,
        doc_type=CACHED_URL_DOCUMENT_TYPE, body=body, id=url)



def add_to_results_cache(universe, hours, results):
    """Cache a set of results under certain number of hours."""
    body = {
        'cached_at': now(stringify=True),
        'hours_since': hours,
        'results': results
    }
    es(universe).index(
        index=RESULTS_CACHE_INDEX,
        doc_type=RESULTS_CACHE_DOCUMENT_TYPE,
        body=body)


def get_score_stats(universe, hours=4):
    """Get extended stats on the scores returned from the results cache.
    :arg hours: type of query to search for."""
    body = {
        'aggregations': {
            'fresh_queries': {
                'filter': {
                    'term': {
                        'hours_since': hours
                    }
                },
                'aggregations': {
                    'scores': {
                        'extended_stats': {
                            'field': 'score'
                        }
                    }
                }
            }
        }
    }
    res = es(universe).search(
        index=RESULTS_CACHE_INDEX, 
        doc_type=RESULTS_CACHE_DOCUMENT_TYPE, 
        body=body)
    return res.aggregations['fresh_queries']['scores']


def get_top_link(universe, hours=4, quantity=5):
    """Search for any links in the current set that are a high enough score
    to get into top links. Return one (and only one) if so."""
    try:
        top_links = get_items(universe, hours=hours, quantity=quantity)
    except IndexError:
        return None
    score_stats = get_score_stats(universe, hours=hours)
    # Treat a link as a top link if it's > 2 standard devs above the average
    if score_stats['avg'] is None:
        return None
    cutoff = score_stats['avg'] + (2 * score_stats['std_deviation'])
    link_is_already_top = lambda link: es(universe).exists(
        index=TOP_CONTENT_INDEX, 
        doc_type=TOP_CONTENT_DOCUMENT_TYPE, 
        id=link['url'])
    for link in top_links:
        if link['score'] >= cutoff and not link_is_already_top(link):
            # We only want one at a time even if more than 1 are in the results
            return link
    return None


def add_to_top_links(universe, link):
    """Index a new top link to the given universe."""
    es(universe).index(
        index=TOP_CONTENT_INDEX, 
        doc_type=TOP_CONTENT_DOCUMENT_TYPE,
        id=link['url'],
        body=link)


def get_recent_top_links(universe, quantity=20):
    """Get the most recently added top links in the given universe."""
    body = {
        'sort': [{
            'tweets.created': {
                'order': 'desc'
            }
        }]
    }
    return es(universe).search(index=TOP_CONTENT_INDEX, 
        doc_type=TOP_CONTENT_DOCUMENT_TYPE, body=body, size=quantity)


def save_content(universe, content):
    """Save the content of a URL to the index."""
    es(universe).index(index=universe,
        doc_type=CONTENT_DOCUMENT_TYPE,
        id=content['url'],
        body=content)


def delete_user(universe, user_id):
    """Delete a user from the universe index by their id."""
    es(universe).delete(index=universe, 
        doc_type=USER_DOCUMENT_TYPE, id=user_id)


def delete_content_by_url(universe, url):
    """Delete the content specified by url."""
    es(universe).delete(index=universe,
        doc_type=CONTENT_DOCUMENT_TYPE, id=url)


def delete_tweets_by_url(universe, url):
    """Delete tweets specified by url."""
    es(universe).delete_by_query(index=universe,
        doc_type=TWEET_DOCUMENT_TYPE,
        body={'query': { 'term': { 'content_url': url }}})


def save_user(universe, user):
    """Check if a user exists in the database. If not, create it.
    If so, update it."""
    kwargs = {
        'index': universe,
        'doc_type': USER_DOCUMENT_TYPE,
        'id': user.get('id_str', user.get('id')),
    }
    if es(universe).exists(**kwargs):
        kwargs['body'] = {'doc': user}
        es(universe).update(**kwargs)
    else:
        kwargs['body'] = user
        es(universe).index(**kwargs)


def get_user_ids(universe, size=None):
    """Get top users for the universe by weight.
    :arg size: number of users to get. Defaults to all users."""
    body = {
        'sort': [{
            'weight': {
                'order': 'desc'
            }
        }]
    }
    user_ids = get_all_docs(universe, 
        index=universe, 
        doc_type=USER_DOCUMENT_TYPE,
        body=body,
        size=size)
    return user_ids


def enqueue_tweet(universe, tweet):
    """Save a tweet to the universe index as an unprocessed tweet document.
    """
    es(universe).index(index=universe,
        doc_type=UNPROCESSED_TWEET_DOCUMENT_TYPE,
        id=tweet['id'],
        body=tweet)


def next_unprocessed_tweet(universe, not_ids=None):
    """Get the next unprocessed tweet and delete it from the index."""
    # TODO: redo this so it is an efficient queue. Currently for
    # testing only.
    try:
        if not_ids is None:
            result = es(universe).search(index=universe,
                doc_type=UNPROCESSED_TWEET_DOCUMENT_TYPE,
                size=1, version=True).next()
        else:
            body = {
                'query': {
                    'bool': {
                        'must_not': {
                            'ids': {
                                'values': not_ids
                            }
                        }
                    }
                }
            }
            result = es(universe).search(index=universe,
                doc_type=UNPROCESSED_TWEET_DOCUMENT_TYPE,
                size=1, version=True, body=body).next()
    except StopIteration:
        # There are no unprocessed tweets in the universe
        return None
    try:
        es(universe).delete(index=universe,
            doc_type=UNPROCESSED_TWEET_DOCUMENT_TYPE,
            id=result._id, version=result._version)
    except NotFoundError:
        # Something's wrong. Ignore it for now.
        logger().info('Could not find raw tweet %s.' % result._id)
        return next_unprocessed_tweet(universe, not_ids=not_ids)
    except ConflictError:
        # Could happen if another processor grabbed and deleted this tweet,
        # or state is otherwise inconsistent.
        logger().info('Version conflict. Skipping raw tweet ID: %s' % (
            result._id))
        if not_ids is None:
            not_ids = [result._id]
        else:
            not_ids.append(result._id)
        return next_unprocessed_tweet(universe, not_ids=not_ids)
    logger().debug('Dequeued raw tweet: %s' % result._id)
    return result


def save_tweet(universe, tweet):
    """Save a tweet to the universe index, fully processed."""
    es(universe).index(index=universe,
        doc_type=TWEET_DOCUMENT_TYPE,
        id=tweet['id'],
        body=tweet)


def get_universe_tweets(universe, query=None, quantity=20, 
                        hours=24, start=None, end=None):
    """
    Get tweets in a given universe.

    :arg query: accepts None, string, or dict. 
        if None, matches all
        if string, searches across the tweets' text for the given string
        if dict, accepts any elasticsearch match query 
        `<http://www.elasticsearch.org/guide/en/elasticsearch/reference/current/query-dsl-match-query.html>`_
    :arg start: accepts int or datetime (timezone-unaware, UTC)
        if int, starts at that many number of hours before now
    :arg end: accepts datetime (timezone-unaware, UTC), defaults to now.
    :arg size: number of tweets to return
    """

    start, end = get_query_dates(start, end, hours)

    # Build query based on what was in the input
    if query is None:
        body = {'query': {'match_all': {}}}
    elif isinstance(query, basestring):
        body = {'query': {'match': {'text': query}}}
    else:
        body = {'query': {'match': query}}

    # Now add date range filter
    body['filter'] = {
        'range': {
            'created': {
                'gte': start,
                'lte': end
            }
        }
    }
    return es(universe).search(index=universe, doc_type=TWEET_DOCUMENT_TYPE,
        body=body, size=quantity)


def search_content(universe, query, size=100):
    """
    Search fulltext of all content across universes for a given string, 
    or a custom match query.

    :arg query: accepts a string or dict
        if string, searches fulltext of all content
        if dict, accepts any elasticsearch match query
        `<http://www.elasticsearch.org/guide/en/elasticsearch/reference/current/query-dsl-match-query.html>`_
    :arg size: number of links to return
    """

    if isinstance(query, basestring):
        query = {'text': query}
    body = {
        'query': {
            'match': query
        }
    }
    return es(universe).search(index=universe, doc_type=CONTENT_DOCUMENT_TYPE,
        body=body, size=size)


def search_items(universe, term, quantity=100):
    """
    Search the text of both tweets and content for a given term and universe,
    and return some items matching one or the other.

    :arg term: search term to use for querying both tweets and content
    :arg quantity: number of items to return
    """

    # Search tweets and content for the given term
    body = {
        'query': {
            'query_string': {
                'query': term,
                'fields': ['title', 'description', 'text', 'tags'],
                'analyzer': 'snowball'
            }
        }
    }
    res = es(universe).search(
        index=universe, 
        doc_type=','.join((CONTENT_DOCUMENT_TYPE, TWEET_DOCUMENT_TYPE)), 
        body=body, 
        size=quantity)
    formatted_results = []
    res = [r for r in res]
    for index, result in enumerate(res):
        if not 'tweets' in result:
            result['tweets'] = []
        if result._type == CONTENT_DOCUMENT_TYPE:
            matching_tweets = filter(
                lambda r: 'content_url' in r and r.content_url == result.url,
                res[index+1:])
            if matching_tweets:
                for tweet in matching_tweets:
                    popped_tweet = res.pop(res.index(tweet))
                    result['tweets'].append(popped_tweet)
        else:
            try:
                matching_content = filter(
                    lambda r: 'url' in r and r.url == result.content_url,
                    res[index+1:])[0]
            except IndexError:
                result = {
                    'type': 'tweet',
                    'url': result.content_url,
                    'tweets': [result]
                }
            else:
                tweet = result
                result = res.pop(res.index(matching_content))
                result['type'] = 'content'
                result['tweets'] = [tweet]
        result['rank'] = index + 1
        if result['tweets']:
            result['first_tweeted'] = get_since_now(
                result['tweets'][0]['created'])
        formatted_results.append(result)
    return formatted_results


def get_user_weights(universe, user_ids):
    """Takes a list of user ids and returns a dict 
    with their weighted influence."""
    res = es(universe).mget({'ids': list(set(user_ids))}, 
        index=universe, doc_type=USER_DOCUMENT_TYPE)
    users = filter(lambda u: u._found, res)
    user_weights = dict([ (user.id, user.weight) for user in users])
    return user_weights


def score_link(link, user_weights, time_decay=True, hours=24):
    """Scores a given link returned from elasticsearch.

    :arg link: full elasticsearch result for the link
    :arg user_weights: a dict with key,value pairs
        key is the user's id, value is the user's weighted twitter influence
    :arg time_decay: whether or not to decay the link's score based on time
    :arg hours: used for determining the decay factor if decay is enabled
    """
    score = 0.0
    score_explanation = []
    convert_weight_to_score = lambda weight: math.log(weight*10 + 1)

    for tweeter in link['tweeters']['buckets']:
        # if they aren't in user_weights, they're no longer in the universe
        user_weight = user_weights.get(tweeter['key'], 0.0)
        tweeter_influence = convert_weight_to_score(user_weight)
        score += tweeter_influence
        score_explanation.append(
            'citizen %s with weight %.2f raises score %.2f to %.2f' % \
            (tweeter['key'], user_weight, tweeter_influence, score))
    if time_decay:
        # The amount to decay the original score by every hour
        # Longer-range searches mean less hourly decay
        DECAY_FACTOR = 1.0 - (1 / float(hours))

        first_tweeted = link['first_tweets']['hits']['hits'][0]['sort'][0]
        minutes_since = get_since_now(first_tweeted, 
            time_type='minute', stringify=False)[0]
        hours_since = minutes_since / 60

        orig_score = score
        velocity = score / (minutes_since + 1)
        for hour in range(hours_since):
            score *= DECAY_FACTOR

        score_explanation.append(
            'decay for %d hours drops score to %.2f (%.2f of original). '\
            'Velocity of %.2f' %\
            (hours_since, score, score/orig_score if orig_score else score, velocity))
    return score, score_explanation


def get_items(universe, quantity=20, hours=24, 
              start=None, end=None, time_decay=True):
    """
    The default function: gets the most popular links shared 
    from a given universe and time frame.

    :arg quantity: number of links to return
    :arg hours: hours since end to search through.
    :arg start: start datetime in UTC. Defaults to hours.
    :arg end: end datetime in UTC. Defaults to now.
    :arg time_decay: whether or not to decay the score based on the time
        of its first tweet.
    """

    start, end = get_query_dates(start, end, hours)
    search_limit = quantity * 5 if time_decay else quantity * 2

    # Get the top links in the given time frame, and some extra agg metadata
    body = {
        'aggregations': {
            'recent_tweets': {
                'filter': {
                    'range': {
                        'created': {
                            'gte': start,
                            'lte': end
                        }
                    }
                },
                'aggregations': {
                    CONTENT_DOCUMENT_TYPE: {
                        'terms': {
                            'field': 'content_url',
                            # This orders by doc count, but we want the
                            # number of (unique) users tweeting it, weighted
                            # by influence. Is there any way to sub-aggregate
                            # that data and order it here?
                            'order': {
                                '_count': 'desc'
                            },
                            # Get extra docs because we need to reorder them
                            'size': search_limit,
                            'min_doc_count': 2,
                        },
                        'aggregations': {
                            'tweeters': {
                                'terms': {
                                    'field': 'user_id',
                                    'size': 1000
                                }
                            },
                            'first_tweets': {
                                'top_hits': {
                                    'size': 3,
                                    'sort': [{
                                        'created': {
                                            'order': 'asc'
                                        }
                                    }]
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    res = es(universe).search(index=universe, doc_type=TWEET_DOCUMENT_TYPE,
        body=body, size=0)
    links = res.aggregations['recent_tweets'][CONTENT_DOCUMENT_TYPE]['buckets']
    # There's no content in the given time frame
    if not links:
        return []
    # Do another filter query to figure out which of these links were tweeted before
    # the given time range.
    body2 = {
        'filter': {
            'and': [{
                'terms': {
                    'content_url': [link['key'] for link in links]
                    }
                }, {
                'range': {
                    'created': {
                        'lte': start
                    }
                }
            }]
        }
    }
    res2 = es(universe).search(index=universe, doc_type=TWEET_DOCUMENT_TYPE,
        body=body2, size=1000)
    outside_of_range = set([h.content_url for h in res2])
    links = filter(lambda link: link['key'] not in outside_of_range, links)
    if not links:
        return []

    # Score each link based on its tweeters' relative influences, and time since
    tweeter_ids = [item for sublist in 
        [[i['key'] for i in link['tweeters']['buckets']] for link in links] 
        for item in sublist]
    user_weights = get_user_weights(universe, tweeter_ids)
    for link in links:
        link['score'], link['score_explanation'] = score_link(
                link, user_weights, time_decay=time_decay, hours=hours)
    sorted_links = sorted(links, 
        key=lambda link: link['score'], reverse=True)[:quantity]

    # Get the full metadata for these urls.
    top_urls = [url['key'] for url in sorted_links]
    link_res = es(universe).mget({'ids': top_urls}, 
        index=universe, doc_type=CONTENT_DOCUMENT_TYPE)
    matching_links = filter(lambda c: c._found, link_res)

    # Add some metadata, including the tweet
    top_links = []
    for index, link in enumerate(matching_links):
        # Add the link's rank
        link['rank'] = index + 1

        # Add the first time the link was tweeted, and the score
        link_match = filter(lambda l: l['key'] == link['url'], links)[0]
        link['score'] = link_match['score']
        link['score_explanation'] = link_match['score_explanation']
        
        tweets = link_match['first_tweets']['hits']['hits']
        link['first_tweeted'] = get_since_now(tweets[0]['sort'][0])
        link['tweets'] = [tweet['_source'] for tweet in tweets]
        top_links.append(link)
    return top_links


def get_top_providers(universe, size=2000):
    """
    Get a list of all providers (i.e. domains) in order of popularity.
    Possible future use for autocomplete, to search across publications.
    """
    body = {
        'aggregations': {
            'providers': {
                'terms': {
                    'field': 'provider',
                    'size': size
                }
            }
        }
    }
    res = es(universe).search(
        index=universe, 
        doc_type=CONTENT_DOCUMENT_TYPE, 
        body=body, 
        size=0)
    return [i['key'] for i in res.aggregations['providers']]


def get_latest_tweet(universe):
    body = {
        'sort': { 'created': { 'order': 'desc' }}
    }
    res = es(universe).search(
        index=universe,
        doc_type=TWEET_DOCUMENT_TYPE,
        body = body)
    try:
        return res.next()
    except StopIteration:
        return None
        

def get_latest_raw_tweet(universe):
    body = {
        'sort': { 'created_at': { 'order': 'desc' }}
    }
    res = es(universe).search(
        index=universe,
        doc_type=UNPROCESSED_TWEET_DOCUMENT_TYPE,
        body = body)
    try:
        return res.next()
    except StopIteration:
        return None
       

