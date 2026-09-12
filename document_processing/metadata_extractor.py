"""AI-assisted metadata enrichment for research papers."""

from shared.models.document import DocumentMetadata


class MetadataExtractor:
    """Uses AI to extract rich metadata from paper text."""

    async def extract(
        self, text: str, base_metadata: DocumentMetadata
    ) -> DocumentMetadata:
        """Enrich metadata with AI-extracted fields (title, authors, abstract, keywords)."""
        ...  # TODO: call Claude with extraction prompt

    async def extract_doi(self, text: str) -> str | None:
        """Extract DOI using regex patterns."""
        ...  # TODO: implement regex

    async def extract_keywords(self, abstract: str) -> list[str]:
        """Extract keywords from abstract text."""
        ...  # TODO: implement
