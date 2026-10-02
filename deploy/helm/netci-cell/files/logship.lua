-- A build log line as Jenkins wrote it, without its console notes (ESC[8mha:...ESC[0m: encoded
-- hyperlinks Jenkins hides in the browser), labelled with the job and build its file belongs to.
function build_line(tag, ts, record)
  local file = record["file"]
  local rel, build = string.match(file, "/jobs/(.+)/builds/([^/]+)/log$")
  if rel == nil then
    return -1, ts, record
  end
  local job = string.gsub(string.gsub(rel, "/jobs/", "/"), "/branches/", "/")
  local line = string.gsub(record["log"] or "", "\27%[8mha:.-\27%[0m", "")
  return 1, ts, {log = line, job = job, build = build}
end
