"""Roster HTML in the three shapes residency sites actually publish."""

TABLE_ROSTER = """
<html><head><title>Internal Medicine Residency - Current Residents</title></head><body>
<h1>Current Residents</h1>
<table>
 <tr><th>Name</th><th>PGY</th><th>Medical School</th><th>Email</th></tr>
 <tr><td>Jane A. Doe, MD</td><td>PGY-2</td><td>Pritzker</td>
     <td><a href="mailto:Jane.Doe@uchicago.edu">Jane.Doe@uchicago.edu</a></td></tr>
 <tr><td>Robert Chen, DO</td><td>PGY-1</td><td>Rush</td>
     <td><a href="mailto:rchen@uchicago.edu">rchen@uchicago.edu</a></td></tr>
 <tr><td>Priya Raman, MBBS</td><td>PGY-3</td><td>AIIMS</td>
     <td>priya [at] uchicago [dot] edu</td></tr>
</table>
<h2>Program Leadership</h2>
<table><tr><th>Name</th><th>Role</th><th>Email</th></tr>
 <tr><td>Alan Grant, MD</td><td>Program Director</td>
     <td><a href="mailto:agrant@uchicago.edu">agrant@uchicago.edu</a></td></tr>
</table></body></html>
"""

CARD_ROSTER = """
<html><head><title>Meet Our Surgery Fellows</title></head><body>
<div class="fellow-card"><img src="a.jpg"><h3>Maria Gonzalez, MD</h3>
  <p>Vascular Surgery Fellow &middot; Class of 2027</p>
  <a href="mailto:mgonzalez@med.example.edu">Email Maria</a></div>
<div class="fellow-card"><img src="b.jpg"><h3>Tom O'Brien</h3>
  <p>Fellow, Class of '28</p>
  <a href="mailto:tobrien@med.example.edu">tobrien@med.example.edu</a></div>
<div class="staff-card"><h3>Susan Lee, MD</h3>
  <p>Attending Physician, Chief of Vascular Surgery</p>
  <a href="mailto:slee@med.example.edu">slee@med.example.edu</a></div>
</body></html>
"""

SPA_SHELL = (
    '<html><head><title>Residents</title></head>'
    '<body><div id="root"></div></body></html>'
)

NO_EMAIL_ROSTER = (
    "<html><head><title>Our Residents</title></head><body>"
    + "<p>Our residents are the heart of the program. </p>" * 30
    + "</body></html>"
)


MIXED_DIRECTORY = """
<html><head><title>Internal Medicine Residency</title></head><body>
<h1>Our Team</h1>
<table>
 <tr><th>Name</th><th>Position</th><th>PGY</th><th>Email</th></tr>
 <tr><td>Jane A. Doe, MD</td><td>Resident</td><td>PGY-2</td>
     <td><a href="mailto:jane.doe@med.example.edu">jane.doe@med.example.edu</a></td></tr>
 <tr><td>Marcus Webb, MD</td><td>Chief Resident</td><td>PGY-3</td><td></td></tr>
 <tr><td>Alan Grant, MD</td><td>Program Director</td><td></td>
     <td><a href="mailto:agrant@med.example.edu">agrant@med.example.edu</a></td></tr>
 <tr><td>Ray Arnold</td><td>Program Coordinator</td><td></td>
     <td><a href="mailto:rarnold@med.example.edu">rarnold@med.example.edu</a></td></tr>
 <tr><td>Tim Murphy</td><td>Medical Student</td><td></td><td></td></tr>
</table>
<div class="faculty-card"><h3>Ellie Sattler, MD</h3><p>Associate Professor</p></div>
</body></html>
"""

ALUMNI_ROSTER = """
<html><head><title>Residency Alumni</title></head><body>
<table>
 <tr><th>Name</th><th>Email</th></tr>
 <tr><td>Gone Person, MD</td>
     <td><a href="mailto:gone@med.example.edu">gone@med.example.edu</a></td></tr>
</table></body></html>
"""


def large_card_roster(count: int = 14) -> str:
    """A roster with many sibling cards.

    Regression guard: block de-duplication once keyed on id(node), and because
    selectolax builds a fresh wrapper per .parent access, garbage-collected ids
    were reused and unrelated cards collided as "already seen". The result was a
    nondeterministic subset of the roster, which looks like success rather than
    failure. Any count below `count` here means that class of bug is back.
    """
    cards = []
    for i in range(count):
        cards.append(
            f'''<li><div class="c-selector-group__ctn">
                 <div class="c-selector-group__name"><h3>Person Number{i} Lastname{i}, MD</h3></div>
                 <p>PGY-{(i % 5) + 1}</p>
                 <a href="mailto:person{i}@med.example.edu">person{i}@med.example.edu</a>
               </div></li>'''
        )
    return (
        "<html><head><title>Current Residents</title></head><body><ul>"
        + "".join(cards)
        + "</ul></body></html>"
    )


# A faculty directory with no mailto links, no person CSS classes and no cards:
# just a run of headings. Seen live on radonc.uchicago.edu/people/our-faculty,
# where 34 people were being missed entirely.
HEADING_ONLY_FACULTY = """
<html><head><title>Our Faculty | Department of Radiation Oncology</title></head>
<body>
<div class="content">
  <h2>Our Faculty</h2>
  <div><h3>Nishant Agrawal, MD</h3><p>Professor</p></div>
  <div><h3>Bulent Aydogan, PhD</h3><p>Professor</p></div>
  <div><h3>Stephanie Bennett</h3><p>Assistant Professor</p></div>
  <div><h3>Jason Bugno, PHARMD PhD</h3><p>Assistant Professor</p></div>
</div>
</body></html>
"""
