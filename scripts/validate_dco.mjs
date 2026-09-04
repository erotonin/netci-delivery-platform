import { execFileSync } from 'node:child_process'
import process from 'node:process'

const range = process.argv[2] || 'HEAD^..HEAD'
let output
try {
  output = execFileSync(
    'git',
    ['log', '--no-merges', '--format=%H%x00%an%x00%ae%x00%B%x1e', range],
    { encoding: 'utf8' },
  )
} catch (error) {
  console.error(`cannot inspect DCO range ${range}: ${error.message}`)
  process.exit(2)
}

const failures = []
let checked = 0
for (const raw of output.split('\x1e')) {
  const record = raw.trim()
  if (!record) continue
  const [sha, authorName, authorEmail, ...bodyParts] = record.split('\x00')
  const body = bodyParts.join('\x00')
  const signoffs = [...body.matchAll(/^Signed-off-by:\s*(.+?)\s*<([^>]+)>\s*$/gim)]
  const matching = signoffs.some(match => (
    match[1].trim().toLowerCase() === authorName.trim().toLowerCase()
    && match[2].trim().toLowerCase() === authorEmail.trim().toLowerCase()
  ))
  checked += 1
  if (!matching) failures.push(`${sha.slice(0, 12)} ${authorName} <${authorEmail}>`)
}

if (!checked) {
  console.error(`DCO range contains no non-merge commits: ${range}`)
  process.exit(2)
}
if (failures.length) {
  console.error('The following commits need an author-matching Signed-off-by line:')
  console.error(failures.join('\n'))
  console.error('Amend each commit with: git commit --amend --signoff')
  process.exit(1)
}

console.log(`DCO passed for ${checked} commit(s) in ${range}`)
