"""Read-only port for the extraction-prediction half of the fidelity instrument."""

from __future__ import annotations

import abc
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from book_graph_rag.domain.extraction_fidelity import Prediction
from book_graph_rag.domain.extraction_labels import LabeledChunk


@dataclass(frozen=True, slots=True)
class ExtractionPredictionMeasurement:
    """What the pipeline attached to the labeled chunks, merge links left intact.

    ``predictions_by_chunk`` is keyed by ``chunk_id`` (``{book_id}:{chunk_index}``)
    and holds every entity the extractor committed for that chunk — **including
    merged entities**, which carry their ``merged_into`` target. ``canonical_of``
    maps a merged id to its target for every merged entity that appears in those
    predictions, and nothing else.

    The adapter deliberately does NOT resolve or collapse merges: protocol §4.3
    resolves and collapses in the metric, and doing it twice would hide the
    difference between "found but merged" and "found canonical". Collapsing here
    would make a duplicate prediction indistinguishable from a single one, which
    is exactly the inflation the metric exists to catch.
    """

    predictions_by_chunk: Mapping[str, list[Prediction]]
    canonical_of: Mapping[str, str]


class ExtractionPredictionPort(abc.ABC):
    """Contract for reading the pipeline's per-chunk entity predictions.

    Read-only by construction: every implementation is a ``MATCH … RETURN`` over
    the graph, never a mutation.
    """

    @abc.abstractmethod
    async def measure(
        self, labeled_chunks: Sequence[LabeledChunk]
    ) -> ExtractionPredictionMeasurement:
        """Return predictions for each labeled chunk plus their merge targets.

        Every labeled chunk must appear in ``predictions_by_chunk`` — a chunk with
        no mentions maps to an empty list, which is a measurement ("the extractor
        found nothing"), not missing data.
        """
        ...
