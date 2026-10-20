"""blas_threads.py — one OpenBLAS thread per process unless the user says otherwise (ADR-054, B-062).

OpenBLAS allocates a buffer for every thread it might use when the library loads,
not when it first runs. The faiss-cpu wheel bundles OpenBLAS, so ``import faiss``
alone committed 2.10 GB on a 14-thread Windows machine, against 0.07 GB at one
thread. ``faiss.omp_set_num_threads`` runs after the import and cannot get it back.

Import this module before anything that imports numpy or faiss: it is the first
import of ``core`` and of every entry module. ``setdefault`` keeps an explicit
``OPENBLAS_NUM_THREADS``. Child processes (the model host, the summarizer worker)
inherit the variable through the environment.

A leaf: ``os`` only. Importing anything else here could pull numpy in first.
"""
import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
