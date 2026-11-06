## See the shape of the code

narration: A vault remembers what was decided about a codebase; it helps to see the codebase too. The code city draws a repository in 3D: every directory a district, every file a building, taller for more lines, coloured by language or by how often it changes.
do: Invoke the gt-visualize skill to draw the demo vault as a city: run `python3 <module:visualize>/gt_visualize.py render $GT_VAULT --redact`, open the file it prints, drag to orbit, and hover the tallest building. Then press Churn and show that colour now means change, not language.
point: The page is one offline file — three.js is inside it, nothing is fetched — and because it was rendered with --redact every file and folder name is a hash, so it can be shared as it is.
