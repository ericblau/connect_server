from ctypes import c_bool
import json
import logging
import multiprocessing
from queue import Empty
from time import sleep

from globus_sdk import GlobusAPIError
import mdf_toolbox

from mdf_connect_server import CONFIG
from .utils import split_source_id


logger = logging.getLogger(__name__)


def search_ingest(feedstock_file, source_id, index, batch_size,
                  num_submitters=CONFIG["NUM_SUBMITTERS"]):
    """Ingests feedstock from file.

    Arguments:
    feedstock_file (str): The feedstock file to ingest.
    source_id (str): The source_id of the feedstock.
    index (str): The Search index to ingest into.
    batch_size (int): Max size of a single ingest operation. -1 for unlimited. Default 100.
    num_submitters (int): The number of submission processes to create. Default NUM_SUBMITTERS.

    Returns:
    dict: success (bool): True on success.
          errors (list): The errors encountered.
          details (str): If success is False, details about the major error, if available.
    """
    ingest_client = mdf_toolbox.confidential_login(
                        mdf_toolbox.dict_merge(CONFIG["GLOBUS_CREDS"],
                                               {"services": ["search_ingest"]}))["search_ingest"]
    index = mdf_toolbox.translate_index(index)
    source_info = split_source_id(source_id)

    # Delete previous version of this dataset in Search
    del_q = {
        "q": "mdf.source_name:{}".format(source_info["source_name"]),
        "advanced": True
    }
    # Try deleting from Search until success or try limit reached
    # Necessary because Search will 5xx but possibly succeed on large deletions
    i = 0
    while True:
        try:
            del_res = ingest_client.delete_by_query(index, del_q)
            break
        except GlobusAPIError as e:
            if i < CONFIG["SEARCH_RETRIES"]:
                logger.warning("{}: Retrying Search delete error: {}".format(source_id, repr(e)))
                i += 1
            else:
                raise
    if del_res["num_subjects_deleted"]:
        logger.info(("{}: {} Search entries cleared from "
                     "{}").format(source_id, del_res["num_subjects_deleted"],
                                  source_info["source_name"]))

    # Set up multiprocessing
    ingest_queue = multiprocessing.Queue()
    error_queue = multiprocessing.Queue()
    input_done = multiprocessing.Value(c_bool, False)

    # Create submitters
    submitters = [multiprocessing.Process(target=submit_ingests,
                                          args=(ingest_queue, error_queue,
                                                index, input_done, source_id))
                  for i in range(num_submitters)]
    # Create queue populator
    populator = multiprocessing.Process(target=populate_queue,
                                        args=(ingest_queue, feedstock_file, batch_size, source_id))
    logger.debug("{}: Search ingestion starting".format(source_id))
    # Start processes
    populator.start()
    [s.start() for s in submitters]

    # Start pulling off any errors
    # Stop when populator is finished
    errors = []
    while populator.exitcode is None:
        try:
            errors.append(json.loads(error_queue.get(timeout=5)))
        except Empty:
            pass
    # Populator is finished, signal submitters
    input_done.value = True

    # Continue fetching errors until first Empty
    try:
        while True:
            errors.append(json.loads(error_queue.get(timeout=5)))
    except Empty:
        pass

    # Wait for submitters to finish
    [s.join() for s in submitters]
    logger.debug("{}: Submitters joined".format(source_id))

    # Fetch remaining errors, if any
    try:
        while True:
            errors.append(json.loads(error_queue.get(timeout=1)))
    except Empty:
        pass

    logger.debug("{}: Search ingestion finished with {} errors".format(source_id, len(errors)))
    return {
        "success": True,
        "errors": errors
    }


def populate_queue(ingest_queue, feedstock_file, batch_size, source_id):
    # Populate ingest queue
    batch = []
    with open(feedstock_file) as feed_in:
        for str_entry in feed_in:
            entry = json.loads(str_entry)
            # Add gmeta-formatted entry to batch
            acl = entry["mdf"].pop("acl")
            # Identifier is source_id for datasets, source_id + scroll_id for records
            if entry["mdf"]["resource_type"] == "dataset":
                iden = entry["mdf"]["source_id"]
            else:
                iden = entry["mdf"]["source_id"] + "." + str(entry["mdf"]["scroll_id"])
            batch.append(mdf_toolbox.format_gmeta(entry, acl=acl, identifier=iden))

            # If batch is appropriate size
            if batch_size > 0 and len(batch) >= batch_size:
                # Format batch into gmeta and put in queue
                full_ingest = mdf_toolbox.format_gmeta(batch)
                ingest_queue.put(json.dumps(full_ingest))
                batch.clear()

        # Ingest partial batch if needed
        if batch:
            full_ingest = mdf_toolbox.format_gmeta(batch)
            ingest_queue.put(json.dumps(full_ingest))
            batch.clear()
    logger.debug("{}: Input queue populated".format(source_id))
    return


def submit_ingests(ingest_queue, error_queue, index, input_done, source_id):
    """Submit entry ingests to Globus Search."""
    ingest_client = mdf_toolbox.confidential_login(
                        mdf_toolbox.dict_merge(CONFIG["GLOBUS_CREDS"],
                                               {"services": ["search_ingest"]}))["search_ingest"]
    while True:
        # Try getting an ingest from the queue
        try:
            ingestable = json.loads(ingest_queue.get(timeout=5))
        # There are no ingests in the queue
        except Empty:
            # If all ingests have been put in the queue (and thus processed), break
            if input_done.value:
                break
            # Otherwise, more ingests are coming, try again
            else:
                continue
        # Ingest, with error handling
        try:
            # Allow retries
            i = 0
            while True:
                try:
                    ingest_res = ingest_client.ingest(index, ingestable)
                    if not ingest_res["acknowledged"]:
                        raise ValueError("Ingest not acknowledged by Search")
                    task_id = ingest_res["task_id"]
                    task_status = "PENDING"  # Assume task starts as pending
                    # While task is not complete, check status
                    while task_status != "SUCCESS" and task_status != "FAILURE":
                        sleep(CONFIG["SEARCH_PING_TIME"])
                        task_res = ingest_client.get_task(task_id)
                        task_status = task_res["state"]
                    break
                except (GlobusAPIError, ValueError) as e:
                    if i < CONFIG["SEARCH_RETRIES"]:
                        logger.warning("{}: Retrying Search ingest error: {}"
                                       .format(source_id, repr(e)))
                        i += 1
                    else:
                        raise
            if task_status == "FAILURE":
                raise ValueError("Ingest failed: " + str(task_res))
            elif task_status == "SUCCESS":
                logger.debug("{}: Search batch ingested: {}"
                             .format(source_id, task_res["message"]))
            else:
                raise ValueError("Invalid state '{}' from {}".format(task_status, task_res))
        except GlobusAPIError as e:
            logger.error("{}: Search ingest error: {}".format(source_id, e.raw_json))
            # logger.debug('Stack trace:', exc_info=True)
            # logger.debug("Full ingestable:\n{}\n".format(ingestable))
            err = {
                "exception_type": str(type(e)),
                "details": e.raw_json
            }
            error_queue.put(json.dumps(err))
        except Exception as e:
            logger.error("{}: Generic ingest error: {}".format(source_id, repr(e)))
            # logger.debug('Stack trace:', exc_info=True)
            # logger.debug("Full ingestable:\n{}\n".format(ingestable))
            err = {
                "exception_type": str(type(e)),
                "details": str(e)
            }
            error_queue.put(json.dumps(err))
    return


def mass_update_search(index, entries=None, subjects=None, convert_func=None,
                       acl=None, overwrite=False):
    """Update multiple entries in Search.

    Note:
        source_id, source_name, and scroll_id must not be updated.

    Note:
        If subjects is supplied, this function will fetch the existing entries
        and update them, so convert_func is required.
        If entries is supplied, convert_func will be run if present.
        It is an error to provide both entries and subjects.

    Arguments:
        index (str): The Search index to ingest into.
        entries (list of dict): The un-updated versions of the entries to update.
                This argument should be None if providing subjects.
                Default None.
        subjects (list of str): The list of subjects to update.
                This argument should be None if providing entries.
                Default None.
        convert_func (function): The conversion/translation function accepting
            an un-updated entry and returning the updated entry.
            Default None, for no translation.
        acl (list of strings): The list of Globus UUIDs allowed to access this entry.
                Default None, if the acl is in the updated_entry.
        overwrite (bool): If True, will overwrite old entries (fields not present in
                the updates entry will be lost).
                If False, will merge the updated_entry with the old entry.
                Default False.
    """
    # Validate arguments
    if entries is not None and subjects is not None:
        return {
            "success": False,
            "error": "You cannot provide both entries and subjects."
        }
    elif subjects is not None and convert_func is None:
        return {
            "success": False,
            "error": "You must provide a convert_func when supplying subjects."
        }
    # Setup
    ingest_client = mdf_toolbox.confidential_login(
                        mdf_toolbox.dict_merge(CONFIG["GLOBUS_CREDS"],
                                               {"services": ["search_ingest"]}))["search_ingest"]
    index = mdf_toolbox.translate_index(index)
    if isinstance(subjects, str):
        subjects = [subjects]
    if isinstance(entries, dict):
        entries = [entries]

    # Subjects - fetch from Search
    if subjects:
        entries = []
        for subject in subjects:
            try:
                entries.append(ingest_client.get_entry(index, subject)["content"][0])
            except Exception as e:
                return {
                    "success": False,
                    "error": "Unable to fetch subjects: {}".format(repr(e))
                }

    # Translate all entries if function supplied
    updated_entries = ([convert_func(entry) for entry in entries]
                       if convert_func else entries)

    for updated_entry in updated_entries:
        # Get subject (redundant for subject-provided calls but a useful double-check)
        try:
            # Identifier is source_id for datasets, source_id + scroll_id for records
            if updated_entry["mdf"]["resource_type"] == "dataset":
                subject = updated_entry["mdf"]["source_id"]
            else:
                subject = (updated_entry["mdf"]["source_id"]
                           + "." + str(updated_entry["mdf"]["scroll_id"]))
        except KeyError as e:
            return {
                "success": False,
                "error": "Unable to derive subject from entry without key " + str(e)
            }
        # Get ACL
        if acl:
            entry_acl = acl
        else:
            try:
                entry_acl = updated_entry["mdf"].pop("acl")
            except KeyError as e:
                return {
                    "success": False,
                    "error": "Unable to derive acl from entry without key " + str(e)
                }
        # Get old entry (should always exist; this is an update)
        # Serves a check against changing source_id or scroll_id for subject-provided calls
        try:
            old_entry = ingest_client.get_entry(index, subject)
        except Exception as e:
            return {
                "success": False,
                "error": repr(e)
            }
        if not overwrite:
            updated_entry = mdf_toolbox.dict_merge(updated_entry, old_entry["content"][0])

        try:
            gmeta_update = mdf_toolbox.format_gmeta(updated_entry, entry_acl, subject)
            update_res = ingest_client.update_entry(index, gmeta_update)
        except Exception as e:
            return {
                "success": False,
                "error": repr(e)
            }
        if not update_res["success"] or update_res["num_documents_ingested"] != 1:
            return {
                "success": False,
                "error": ("Update returned '{}', "
                          "{} entries were updated.").format(update_res["success"],
                                                             update_res["num_documents_ingested"])
            }
    return {
        "success": True,
        "entries": updated_entries
    }


def update_search_entry(index, updated_entry, subject=None, acl=None, overwrite=False):
    """Update an entry in Search.
    Arguments:
    index (str): The Search index to ingest into.
    updated_entry (dict): The updated version of the entry (not in GMetaFormat).
    subject (str): The identifier for the entry, used to find the old entry.
                   If there are no matches, the update will fail.
                   Default None, to derive the subject from the updated_entry.
    acl (list of strings): The list of Globus UUIDs allowed to access this entry.
                           Default None, if the acl is in the updated_entry.
    overwrite (bool): If True, will overwrite old entry (fields not present in updated_entry
                        will be lost).
                      If False, will merge the updated_entry with the old entry.
                      Default False.

    Returns:
    dict:
        success (bool): True when successful, False otherwise.
        entry (dict): If success is True, contains the entry as it now stands in Search.
                      Otherwise missing.
        error (str): If success is False, contains an error message about the failure.
                     Otherwise missing.
    """
    ingest_client = mdf_toolbox.confidential_login(
                        mdf_toolbox.dict_merge(CONFIG["GLOBUS_CREDS"],
                                               {"services": ["search_ingest"]}))["search_ingest"]
    index = mdf_toolbox.translate_index(index)

    if not subject:
        try:
            # Identifier is source_id for datasets, source_id + scroll_id for records
            if updated_entry["mdf"]["resource_type"] == "dataset":
                subject = updated_entry["mdf"]["source_id"]
            else:
                subject = (updated_entry["mdf"]["source_id"]
                           + "." + str(updated_entry["mdf"]["scroll_id"]))
        except KeyError as e:
            return {
                "success": False,
                "error": "Unable to derive subject from entry without key " + str(e)
            }
    if not acl:
        try:
            acl = updated_entry["mdf"].pop("acl")
        except KeyError as e:
            return {
                "success": False,
                "error": "Unable to derive acl from entry without key " + str(e)
            }

    try:
        old_entry = ingest_client.get_entry(index, subject)
    except Exception as e:
        return {
            "success": False,
            "error": repr(e)
        }
    if not overwrite:
        updated_entry = mdf_toolbox.dict_merge(updated_entry, old_entry["content"][0])

    try:
        gmeta_update = mdf_toolbox.format_gmeta(updated_entry, acl, subject)
        update_res = ingest_client.update_entry(index, gmeta_update)
    except Exception as e:
        return {
            "success": False,
            "error": repr(e)
        }
    if not update_res["success"] or update_res["num_documents_ingested"] != 1:
        return {
            "success": False,
            "error": ("Update returned '{}', "
                      "{} entries were updated.").format(update_res["success"],
                                                         update_res["num_documents_ingested"])
        }
    return {
        "success": True,
        "entry": updated_entry
    }