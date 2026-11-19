# Emit only fixed operation/category/status fields from safe fallback warning lines.
/provider fallback operation=/ {
  operation = ""
  category = ""
  status = ""
  for (i = 1; i <= NF; i++) {
    if ($i ~ /^operation=/) {
      operation = $i
      sub(/^operation=/, "", operation)
    } else if ($i ~ /^category=/) {
      category = $i
      sub(/^category=/, "", category)
    } else if ($i ~ /^http_status=/) {
      status = $i
      sub(/^http_status=/, "", status)
    }
  }
  if ((operation == "bundle" || operation == "guide:job" || operation == "guide:blueprint") && category ~ /^[A-Za-z][A-Za-z0-9_]*$/ && status ~ /^([0-9]+|-)$/) {
    key = operation "|" category "|" status
    count[key]++
  }
}
END {
  for (key in count) {
    split(key, part, "|")
    printf "provider_error operation=%s category=%s http_status=%s count=%d\n", part[1], part[2], part[3], count[key]
  }
}
