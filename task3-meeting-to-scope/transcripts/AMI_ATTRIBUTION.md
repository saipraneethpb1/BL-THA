# AMI Meeting Corpus: attribution

`ami_ES2002b.txt`, `ami_ES2002c.txt` and `ami_ES2002d.txt` are derived from the **AMI Meeting Corpus** (manual annotations release 1.6.2), © the AMI Consortium, used under the [Creative Commons Attribution 4.0 International licence (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/).

- **Source:** https://groups.inf.ed.ac.uk/ami/corpus/ (`ami_public_manual_1.6.2.zip`)
- **Changes made:** the word-level XML transcripts (`words/ES2002*.words.xml`) were converted into plain text. Each speaker's words were grouped into turns, a new turn starting after a pause of 2 seconds or more, and the turns were ordered by start time. No words were added or edited.
- **Gold labels** in `evaluation/gold/ami_*.json` come from the AMI annotators' "decisions" lists in `abstractive/ES2002*.abssumm.xml`, turned into keyword checks.

Reference: Carletta, J. et al. (2005). *The AMI Meeting Corpus: A Pre-announcement.* Machine Learning for Multimodal Interaction (MLMI).
