import hashlib

from metaflow.client.cache import CacheAction
from .utils import NoRetryS3
from .utils import MetaflowS3CredentialsMissing, MetaflowS3AccessDenied, MetaflowS3Exception, MetaflowS3NotFound, MetaflowS3URLException
from .utils import decode, batchiter
import json

MAX_SIZE = 4096
S3_BATCH_SIZE = 512

class SearchArtifacts(CacheAction):
    '''
    Fetches artifacts by locations and performs a search against the object contents.
    Caches artifacts based on location, and search results based on a combination of query&artifacts searched

    Returns:
        {
            "s3_location": {
                "included": boolean,
                "matches": boolean
            }
        }
    matches: determines whether object content matched search term

    included: denotes if the object content was able to be included in the search (accessible or not)
    '''

    @classmethod
    def format_request(cls, locations, searchterm):
        unique_locs = list(frozenset(locations))
        msg = {
            'artifact_locations': unique_locs,
            'searchterm': searchterm
        }
        
        artifact_keys = []
        for location in unique_locs:
            artifact_keys.append(artifact_cache_id(location))
        
        request_id = lookup_id(locations, searchterm)
        stream_key = 'search:stream:%s' % request_id
        result_key = 'search:result:%s' % request_id

        return msg,\
               [result_key, *artifact_keys],\
               stream_key,\
               [stream_key, result_key]


    @classmethod
    def response(cls, keys_objs):
        '''Action should respond with a dictionary of 
        {
            location: {
                "matches": boolean,
                "included": boolean
            }
        }
        that tells the client whether the search term matches in the given location, or if performing search was impossible'''
        return [ json.loads(val) for key, val in keys_objs.items() if key.startswith('search:result') ][0]

    @classmethod
    def stream_response(cls, it):
        for msg in it:
            if msg is None:
                yield msg
            else:
                yield {'event': msg}

    @classmethod
    def execute(cls,
                message=None,
                keys=None,
                existing_keys={},
                stream_output=None,
                **kwargs):

        results = {}
        locations = message['artifact_locations']

        artifact_keys = [key for key in keys if key.startswith('search:artifactdata')]
        result_key = [ key for key in keys if key.startswith('search:result')][0]

        # Lambdas for streaming status updates.
        stream_progress = lambda num: stream_output({"type": "progress", "fraction": num})
        stream_error = lambda err, id: stream_output({"type": "error", "message": err, "id": id})
        
        # Make a list of artifact locations that require fetching (not cached previously)
        locations_to_fetch = [loc for loc in locations if not artifact_cache_id(loc) in existing_keys]

        # Fetch the S3 locations data
        num_s3_batches = max(1, len(locations_to_fetch) // S3_BATCH_SIZE)
        s3_locations = [ loc for loc in locations_to_fetch if loc.startswith("s3://") ]
        with NoRetryS3() as s3:
            for i, locations in enumerate(batchiter(s3_locations, S3_BATCH_SIZE), start=1):
                stream_progress(i / num_s3_batches)
                try:
                    for artifact_data in s3.get_many(locations, return_missing=True):
                        artifact_key = artifact_cache_id(artifact_data.url)
                        if artifact_data.size < MAX_SIZE:
                            try:
                                # TODO: Figure out a way to store the artifact content without decoding?
                                # presumed that cache_data/tmp/ does not persist as long as the cached items themselves,
                                # so we can not rely on the file existing if we only return a filepath as a cached response
                                results[artifact_key] = json.dumps([True, decode(artifact_data.path)])
                            except Exception as ex:
                                # Exceptions might be fixable with configuration changes or other measures,
                                # therefore we do not want to write anything to the cache for these artifacts.
                                stream_error(str(ex), "artifact-handle-failed")
                        else:
                            results[artifact_key] = json.dumps([False, 'object is too large'])
                except MetaflowS3AccessDenied as ex:
                    stream_error(str(ex), "s3-access-denied")
                except MetaflowS3NotFound as ex:
                    stream_error(str(ex), "s3-not-found")
                except MetaflowS3URLException as ex:
                    stream_error(str(ex), "s3-bad-url")
                except MetaflowS3CredentialsMissing as ex:
                    stream_error(str(ex), "s3-missing-credentials")
                except MetaflowS3Exception as ex:
                    stream_error(str(ex), "s3-generic-error")
        # Skip the inaccessible locations
        other_locations = [ loc for loc in locations_to_fetch if not loc.startswith("s3://") ]
        for loc in other_locations:
            artifact_key = artifact_cache_id(loc)
            stream_error("Artifact is not accessible", "artifact-not-accessible")
            results[artifact_key] = json.dumps([False, 'object is not accessible'])

        # Perform search on loaded artifacts.
        search_results = {}
        searchterm = message['searchterm']
        format_loc = lambda x: x[len("search:artifactdata:"):] # extract location from the artifact cache key
        for key in artifact_keys:
            if key in results:
                load_success, value = json.loads(results[key])
            elif key in existing_keys:
                load_success, value = json.loads(existing_keys[key])
            else:
                load_success, value = False, None

            search_results[format_loc(key)] = {
                "included": load_success,
                "matches": value==searchterm
            }
        
        results[result_key] = json.dumps(search_results)

        return results

def lookup_id(locations, searchterm):
    "construct a unique id to be used with stream_key and result_key"
    _string = "-".join(locations)+searchterm
    return hashlib.sha1(_string.encode('utf-8')).hexdigest()

def artifact_cache_id(location):
    "construct a unique cache key for artifact location"
    return 'search:artifactdata:%s' % location