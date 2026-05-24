-- Dump every open Google Chrome tab as delimited records.
-- Fields are separated by U+001F, records by U+001E.
-- Per tab: window index, tab index, URL, title, page-text snippet.
-- The snippet uses `execute javascript`, which needs Chrome's
-- View > Developer > "Allow JavaScript from Apple Events" enabled.
-- If that's off, the snippet is simply empty (wrapped in try).

tell application "Google Chrome"
	set fieldSep to (character id 31)
	set recordSep to (character id 30)
	set output to ""
	set winIndex to 0
	repeat with w in windows
		set winIndex to winIndex + 1
		set tabIndex to 0
		repeat with t in tabs of w
			set tabIndex to tabIndex + 1
			set theURL to ""
			set theTitle to ""
			set snippet to ""
			try
				set theURL to (URL of t) as text
			end try
			try
				set theTitle to (title of t) as text
			end try
			try
				set snippet to ((execute t javascript "(document.body?document.body.innerText:'').substring(0,4000)") as text)
			end try
			set output to output & winIndex & fieldSep & tabIndex & fieldSep & theURL & fieldSep & theTitle & fieldSep & snippet & recordSep
		end repeat
	end repeat
	return output
end tell
