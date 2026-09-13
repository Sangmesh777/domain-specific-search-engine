"""
Development corpus seeder.

Why this exists
---------------
The live regression suite (tests/test_phase12_live.py) exercises a running
server and finishes with a corpus-health assertion:

    assert data["content_terms"] > 0

The suite deletes every document it creates, so that assertion only holds
when the server already owns a persistent corpus. `data/` is gitignored,
which means a fresh clone has no corpus and the gate cannot be reproduced.

This tool builds a small, deterministic, permanent development corpus and
uploads it through the real HTTP API, so that:

    python -m pytest -q            -> the full documented gate

is reproducible on any machine, and so that parity checks such as
`network`, `pdf`, `"network"`, `999`, `BCS502`, pagination and empty query
have real documents to match.

Usage
-----
    python tools/seed_dev_corpus.py                  # build + upload
    python tools/seed_dev_corpus.py --generate-only  # only write files
    python tools/seed_dev_corpus.py --out DIR        # choose output dir

Seeding is idempotent: re-uploading replaces the same filenames.
"""

import argparse
import os
import sys
import tempfile

sys.path.insert(
    0,
    os.path.dirname(os.path.abspath(__file__)),
)

from pdf_writer import write_pdf


DEFAULT_BASE_URL = "http://127.0.0.1:5000"


# ============================================================
# CORPUS DEFINITION
# ============================================================

PDF_DOCUMENTS = {

    "BCS502 Module 2.pdf": [
        (
            "BCS502 Computer Network Technologies Module 2\n"
            "Network layer design and routing.\n"
            "The network layer forwards packets across "
            "an internet network."
        ),
        (
            "Routing protocols and congestion control.\n"
            "Distance vector routing and link state routing "
            "are compared in this module.\n"
            "Internal reference code 999 applies to the "
            "network laboratory assignment."
        ),
        (
            "Transport layer overview.\n"
            "TCP and UDP provide end to end network "
            "communication for applications."
        ),
    ],

    "BCS502 Module 1.pdf": [
        (
            "BCS502 Computer Network Technologies Module 1\n"
            "Introduction to network architectures.\n"
            "The OSI reference model describes seven layers."
        ),
        (
            "Physical layer and transmission media.\n"
            "Guided and unguided media carry network signals."
        ),
    ],

    "BCS502 Module 3.pdf": [
        (
            "BCS502 Computer Network Technologies Module 3\n"
            "Application layer protocols.\n"
            "DNS HTTP SMTP and FTP are network applications."
        ),
        (
            "Network security fundamentals.\n"
            "Encryption protects data crossing a public network."
        ),
    ],

    "Network Security Notes.pdf": [
        (
            "Network Security Notes\n"
            "Firewalls filter network traffic between zones.\n"
            "Intrusion detection monitors the network."
        ),
        (
            "Cryptographic primitives.\n"
            "Symmetric and asymmetric keys secure a network "
            "session against eavesdropping."
        ),
    ],

    "Machine Learning Introduction.pdf": [
        (
            "Machine Learning Introduction\n"
            "Supervised learning trains a model on labelled data.\n"
            "Feature vectors describe every training example."
        ),
        (
            "Model evaluation.\n"
            "Precision recall and accuracy measure classifier "
            "quality on a held out validation set."
        ),
    ],
}


DOCX_DOCUMENTS = {

    "Computer Network Technologies.docx": [
        "Computer Network Technologies",
        "A network is a set of interconnected devices.",
        "Switching and routing move data across the network.",
        "Bandwidth and latency describe network performance.",
    ],

    "Data Structures and Algorithms.docx": [
        "Data Structures and Algorithms",
        "Arrays lists stacks and queues are linear structures.",
        "Trees and graphs are non linear structures.",
        "Algorithm complexity is expressed using big O notation.",
    ],

    "Operating Systems Summary.docx": [
        "Operating Systems Summary",
        "Process scheduling decides which process runs next.",
        "Virtual memory pages physical memory on demand.",
        "File systems organize persistent storage.",
    ],

    "Cloud Computing Basics.docx": [
        "Cloud Computing Basics",
        "Infrastructure platform and software are service models.",
        "Virtualization isolates workloads on shared hardware.",
        "Elasticity allows capacity to grow with demand.",
    ],
}


TXT_DOCUMENTS = {

    "DBMS Unit 4 Notes.txt": (
        "DBMS Unit 4 Notes\n"
        "Normalization removes redundancy from relations.\n"
        "First second and third normal form are progressive rules.\n"
        "Transactions provide atomicity consistency isolation durability.\n"
    ),

    "Software Engineering Lecture Notes.txt": (
        "Software Engineering Lecture Notes\n"
        "The waterfall model completes each phase before the next.\n"
        "Agile methods deliver software in short iterations.\n"
        "Testing verifies behavior against requirements.\n"
    ),

    "Web Technologies Reference.txt": (
        "Web Technologies Reference\n"
        "HTML describes document structure.\n"
        "CSS describes presentation.\n"
        "JavaScript describes behavior in the browser.\n"
    ),

    "Cyber Security Fundamentals.txt": (
        "Cyber Security Fundamentals\n"
        "Confidentiality integrity and availability form the triad.\n"
        "Threat actors exploit software vulnerabilities.\n"
        "Defense in depth layers multiple controls.\n"
    ),
}


def corpus_filenames():
    """Return the deterministic corpus filenames in upload order."""

    return (
        list(PDF_DOCUMENTS)
        + list(DOCX_DOCUMENTS)
        + list(TXT_DOCUMENTS)
    )


# ============================================================
# CORPUS GENERATION
# ============================================================

def build_corpus(directory):
    """
    Write every corpus document into directory.

    Returns the list of generated absolute paths.
    """

    import docx

    os.makedirs(directory, exist_ok=True)

    generated = []

    for filename, pages in PDF_DOCUMENTS.items():

        path = os.path.join(directory, filename)

        write_pdf(path, pages)

        generated.append(path)

    for filename, paragraphs in DOCX_DOCUMENTS.items():

        path = os.path.join(directory, filename)

        document = docx.Document()

        for paragraph in paragraphs:
            document.add_paragraph(paragraph)

        document.save(path)

        generated.append(path)

    for filename, body in TXT_DOCUMENTS.items():

        path = os.path.join(directory, filename)

        with open(
            path,
            "w",
            encoding="utf-8",
        ) as file:
            file.write(body)

        generated.append(path)

    return generated


# ============================================================
# UPLOAD
# ============================================================

def upload_corpus(paths, base_url):
    """Upload the generated corpus through the real HTTP API."""

    import requests

    handles = []

    multipart = []

    try:

        for path in paths:

            handle = open(path, "rb")

            handles.append(handle)

            multipart.append((
                "file",
                (
                    os.path.basename(path),
                    handle,
                    "application/octet-stream",
                ),
            ))

        response = requests.post(
            f"{base_url}/api/upload",
            files=multipart,
            timeout=300,
        )

        response.raise_for_status()

        return response.json()

    finally:

        for handle in handles:
            handle.close()


def read_status(base_url):
    import requests

    response = requests.get(
        f"{base_url}/api/status",
        timeout=60,
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# ENTRY POINT
# ============================================================

def main(argv=None):

    parser = argparse.ArgumentParser(
        description=(
            "Generate and upload the development corpus."
        ),
    )

    parser.add_argument(
        "--out",
        default=None,
        help=(
            "Directory for generated documents "
            "(default: a temporary directory)."
        ),
    )

    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="Flask API base URL.",
    )

    parser.add_argument(
        "--generate-only",
        action="store_true",
        help="Write documents without uploading.",
    )

    arguments = parser.parse_args(argv)

    directory = (
        arguments.out
        or tempfile.mkdtemp(prefix="vtu_dev_corpus_")
    )

    paths = build_corpus(directory)

    print(f"[SEED] Generated {len(paths)} documents in {directory}")

    if arguments.generate_only:

        for path in paths:
            print(f"[SEED]   {os.path.basename(path)}")

        return 0

    result = upload_corpus(paths, arguments.base_url)

    print(
        "[SEED] Upload: "
        f"{result['created_count']} created, "
        f"{result['replaced_count']} replaced, "
        f"{result['rejected_count']} rejected, "
        f"{result['failed_count']} failed"
    )

    status = read_status(arguments.base_url)

    print(
        "[SEED] Status: "
        f"documents={status['documents']} "
        f"content_terms={status['content_terms']} "
        f"filenames_indexed={status['filenames_indexed']} "
        f"page_text_entries={status['page_text_entries']}"
    )

    if result["failed_count"] or result["rejected_count"]:
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
