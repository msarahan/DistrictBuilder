"""
Define the models used by the redistricting app.

The classes in redistricting.models define the data models used in the 
application. Each class relates to one table in the database; foreign key
fields may define a second, intermediate table to map the records to one
another.

This file is part of The Public Mapping Project
http://sourceforge.net/projects/publicmapping/

License:
    Copyright 2010 Micah Altman, Michael McDonald

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

Author: 
    Andrew Jennings, David Zwarg, Kenny Shepard
"""

from django.core.exceptions import ValidationError
from django.contrib.gis.db import models
from django.contrib.gis.gdal import DataSource
from django.contrib.gis.geos import MultiPolygon,Polygon,GEOSGeometry,GEOSException,GeometryCollection,Point
from django.contrib.auth.models import User
from django.db.models import Sum, Max, Q, Count
from django.db.models.signals import pre_save, post_save, m2m_changed
from django.db import connection, transaction
from django.forms import ModelForm
from django.conf import settings
from django.utils import simplejson as json
from django.template.loader import render_to_string
from django.contrib.comments.models import Comment
from django.contrib.contenttypes.models import ContentType
from redistricting.calculators import Schwartzberg, Contiguity
from tagging.models import TaggedItem
from datetime import datetime
from copy import copy
from decimal import *
import sys, cPickle, traceback, types, tagging

class Subject(models.Model):
    """
    A classification of a set of Characteristics.

    A Subject classifies theC haracteristics of a Geounit. Or, each Geounit
    has one Characteristic per Subject.

    If you think about it in GIS terms: 
        a Geounit is a Feature,
        a Subject is an Attribute on a Geounit, and
        a Characteristic is a Data Value for a Subject.
    """

    # The name of the subject (POPTOT)
    name = models.CharField(max_length=50)

    # The display name of the subject (Total Population)
    display = models.CharField(max_length=200, blank=True)

    # A short display name of the subject (Tot. Pop.)
    short_display = models.CharField(max_length = 25, blank=True)

    # A description of this subject
    description = models.CharField(max_length= 500, blank=True)

    # If this subject should be displayed as a percentage,
    # a district's value for this subject will be divided by
    # the value for the given subject.
    # A null value indicates that the subject is not a percentage
    percentage_denominator = models.ForeignKey('Subject',null=True,blank=True)

    # A flag that indicates if this subject should be displayed.
    is_displayed = models.BooleanField(default=True)

    # The position that this subject should be in, relative to all other
    # Subjects, when viewing the subjects in a list.
    sort_key = models.PositiveIntegerField(default=1)

    # The way this Subject's values should be represented.
    format_string = models.CharField(max_length=50, blank=True)

    class Meta:
        """
        Additional information about the Subject model.
        """

        # The default method of sorting Subjects should be by 'sort_key'
        ordering = ['sort_key']

    def __unicode__(self):
        """
        Represent the Subject as a unicode string. This is the Geounit's 
        display name.
        """
        return self.display


class LegislativeBody(models.Model):
    """
    A legislative body that plans belong to. This is to support the
    scenario where one application is supporting both "Congressional"
    and "School District" contests, for example.
    """

    # The name of this legislative body
    name = models.CharField(max_length=256)

    # The name of the units in a plan -- "Districts", for example.
    member = models.CharField(max_length=32)

    # The maximum number of districts in this body
    max_districts = models.PositiveIntegerField()

    # Whether or not districts of this legislative body are allowed multi-members
    multi_members_allowed = models.BooleanField(default=False)
    
    # The format to be used for displaying a map label of a multi-member district.
    # This format string will be passed to python's 'format' function with the named
    # arguments: 'name' (district name) and 'num_members' (number of representatives)
    # For example: "{name} - [{num_members}]" will display "District 5 - [3]" for a district named
    # "District 5" that is configured with 3 representatives.
    multi_district_label_format = models.CharField(max_length=32, default='{name} - [{num_members}]')

    # The minimimum number of multi-member districts allowed in a plan.
    min_multi_districts = models.PositiveIntegerField(default=0)
    
    # The maximum number of multi-member districts allowed in a plan.
    max_multi_districts = models.PositiveIntegerField(default=0)
    
    # The minimimum number of members allowed in a multi-member district.
    min_multi_district_members = models.PositiveIntegerField(default=0)
    
    # The maximimum number of members allowed in a multi-member district.
    max_multi_district_members = models.PositiveIntegerField(default=0)

    # The minimumum total number of members allowed in a plan.
    min_plan_members = models.PositiveIntegerField(default=0)
    
    # The maximumum total number of members allowed in a plan.
    max_plan_members = models.PositiveIntegerField(default=0)

    def get_default_subject(self):
        """
        Get the default subject for display. This is related to the
        LegislativeBody via the LegislativeDefault table.

        Returns:
            The default subject for the legislative body.
        """
        ldef = self.legislativedefault_set.all()
        return ldef[0].target.subject

    def get_base_geolevel(self):
        """
        Get the base geolevel for this legislative body. Each legislative
        body contains multiple geolevels, which are nested. There is only
        one parent geolevel per legislative body, the one with no parent
        above it.

        Returns:
            The base geolevel in this legislative body.
        """
        subj = self.get_default_subject()
        levels = self.legislativelevel_set.filter(target__subject=subj,parent=None)
        return levels[0].geolevel.id

    def get_geolevels(self):
        """
        Get the geolevel heirarchy for this legislative body. This returns
        a list of geolevels that exist in the legislative body, in the
        order in which they are related.
        """
        subject = self.get_default_subject()
        geobodies = self.legislativelevel_set.filter(target__subject=subject)

        ordered = []
        allgeobodies = len(geobodies)
        while len(ordered) < allgeobodies:
            foundbody = False
            for geobody in geobodies:
                if len(ordered) == 0 and geobody.parent is None:
                    # add the first geobody (the one with no parent)
                    ordered.append(geobody)
                    foundbody = True
                elif len(ordered) > 0 and ordered[len(ordered)-1] == geobody.parent:
                    # add the next geobody if it's parent matches the last
                    # geobody appended
                    ordered.append(geobody)
                    foundbody = True

            if not foundbody:
                allgeobodies -= 1

        def glonly(item):
            return item.geolevel

        ordered = map(glonly,ordered)

        ordered.reverse()
        return ordered

    def is_below(self, legislative_body):
        """
        Compares this legislative body to a second legislative body, and
        determines the nesting order (which one is above or below). This
        assumes the relationship can be determined from max_districts.
        
        Parameters:
            legislative_body -- The LegislativeBody in which to perform the comparison

        Returns:
            True if this this legislative body is below the one passed in, False otherwise
        """
        return self.max_districts > legislative_body.max_districts

    def __unicode__(self):
        """
        Represent the LegislativeBody as a unicode string. This is the 
        LegislativeBody's name.
        """
        return self.name

    class Meta:
        verbose_name_plural = "Legislative bodies"


class Geolevel(models.Model):
    """
    A geographic classification for Geounits.

    For example, a Geolevel is the concept of 'Counties', where each 
    Geounit is an instance of a county.  There are many Geounits at a
    Geolevel.
    """

    # The name of the geolevel
    name = models.CharField(max_length = 50)

    # Each geolevel has a maximum and a minimum zoom level at which 
    # features on the map can be selected and added to districts

    # The minimum zoom level
    min_zoom = models.PositiveIntegerField(default=0)

    # The position that this geolevel should be in, relative to all other
    # geolevels, when viewing the geolevels in a list.
    sort_key = models.PositiveIntegerField(default=1)

    # The geographic tolerance of this geographic level, for simplification
    tolerance = models.FloatField(default=10)

    class Meta:
        """
        Additional information about the Subject model.
        """

        # The default method of sorting Geolevels should be by 'sort_key'
        ordering = ['sort_key']

    def __unicode__(self):
        """
        Represent the Geolevel as a unicode string. This is the Geolevel's 
        name.
        """
        return self.name


class LegislativeDefault(models.Model):
    """
    The default settings for a legislative body.
    """

    # The legislative body
    legislative_body = models.ForeignKey(LegislativeBody)

    # The subject for characteristics in this body
    target = models.ForeignKey('Target')

    class Meta:
        unique_together = ('legislative_body',)

    def __unicode__(self):
        return '%s - %s' % (self.legislative_body.name, self.target)


class LegislativeLevel(models.Model):
    """
    A geographic classification in a legislative body.

    A geographic classification can be "Counties", and this classification
    can exist in both "State Senate" and "State House" legislative
    bodies.
    """

    # The geographic classification
    geolevel = models.ForeignKey(Geolevel)

    # The legislative body
    legislative_body = models.ForeignKey(LegislativeBody)

    # Parent geographic classification in this legislative level
    parent = models.ForeignKey('LegislativeLevel',null=True,blank=True)

    # The target that refers to this level
    target = models.ForeignKey('Target',null=True)

    def __unicode__(self):
        """
        Represent the LegislativeLevel as a unicode string. This is the
        LegislativeLevel's LegislativeBody and Geolevel
        """
        return "%s, %s, %s" % (self.legislative_body.name, self.geolevel.name, self.target)

    class Meta:
        unique_together = ('geolevel','legislative_body','target',)


class Geounit(models.Model):
    """
    A discrete areal unit.

    A Geounit represents an area at a Geolevel. There are many Geounits at
    a Geolevel. If 'Counties' was a Geolevel, 'Adams County' would be a
    Geounit at that Geolevel.
    """

    # The name of the geounit. If a geounit doesn't have a name (in the
    # instance of a census tract or block), this can be the ID or FIPS code.
    name = models.CharField(max_length=200)

    # The field used when exporting or importing plans from District Index Files
    portable_id = models.CharField(max_length=50, db_index=True, blank=True, null=True)

    # An identifier used by the data ingestion process.  This number is a
    # concatenated series of identifiers identifying parent-child relationships
    tree_code = models.CharField(max_length=50, db_index=True, blank=True, null=True)

    # The ID of the geounit that contains this geounit
    child = models.ForeignKey('Geounit',null=True,blank=True)

    # The full geometry of the geounit (high detail).
    geom = models.MultiPolygonField(srid=3785)

    # The lite geometry of the geounit (generated from geom via simplify).
    simple = models.MultiPolygonField(srid=3785)

    # The centroid of the geometry (generated from geom via centroid).
    center = models.PointField(srid=3785)

    # The geographic level of this Geounit
    geolevel = models.ForeignKey(Geolevel)

    # Manage the instances of this class with a geographically aware manager
    objects = models.GeoManager()

    @staticmethod
    def get_mixed_geounits(geounit_ids, legislative_body, geolevel, boundary, inside):
        """
        Spatially search for the largest Geounits inside or outside a 
        boundary.

        Search for Geounits in a multipass search. The searching method
        gets Geounits inside a boundary at a Geolevel, then determines
        if there is a geographic remainder, then repeats the search at
        a smaller Geolevel inside the specified LegislativeBody, until 
        the base Geolevel is reached.

        Parameters:
            geounit_ids -- A list of Geounit IDs. Please note that these
                must be strings, not integers.
            legislative_body -- The LegislativeBody that contains this 
                geolevel.
            geolevel -- The ID of the Geolevel that contains geounit_ids
            boundary -- The GEOSGeometry that defines the edge of the
                spatial search area.
            inside -- True or False to search inside or outside of the 
                boundary, respectively.

        Returns:
            A list of Geounit objects, with the ID, child, geolevel,
            and Geometry fields populated.
        """
        if not boundary and inside:
            # there are 0 geounits inside a non-existant boundary
            return []
            
        # Make sure the geolevel is a number
        geolevel = int(geolevel)
        levels = legislative_body.get_geolevels()
        base_geolevel = levels[len(levels)-1]
        selection = None
        units = []
        searching = False
        for level in levels:
            # if this geolevel is the requested geolevel
            if geolevel == level.id:
                searching = True
                guFilter = Q(id__in=geounit_ids)

                # Get the area defined by the union of the geounits
                selection = Geounit.objects.filter(guFilter).collect()

                selection = enforce_multi(selection,collapse=True)
               
                # Begin crafting the query to get the id and geom
                query = "SELECT id,child_id,geolevel_id,st_ashexewkb(geom,'NDR') FROM redistricting_geounit WHERE id IN (%s) AND " % (','.join(geounit_ids))

                # create a boundary if one doesn't exist
                if not boundary:
                    boundary = empty_geom(selection.srid)

                if inside:
                    # Searching inside the boundary
                    if level != base_geolevel:
                        # Search by geometry
                        query += "st_within(geom, geomfromewkt('%s'))" % boundary.ewkt
                    else:
                        # Search by centroid
                        query += "st_intersects(center, geomfromewkt('%s'))" % boundary.ewkt
                else:
                    # Searching outside the boundary
                    if level != base_geolevel:
                        # Search by geometry
                        query += "NOT st_intersects(geom, geomfromewkt('%s'))" % boundary.ewkt
                    else:
                        # Search by centroid
                        query += "NOT st_intersects(center, geomfromewkt('%s'))" % boundary.ewkt

                # Execute our custom SQL
                cursor = connection.cursor()
                cursor.execute(query)
                rows = cursor.fetchall()
                count = 0
                for row in rows:
                    count += 1
                    geom = GEOSGeometry(row[3])
                    # Create a geounit, and add it to the list of units
                    units.append(Geounit(id=row[0],geom=geom,child_id=row[1],geolevel_id=row[2]))

                # if we're at the base level, and haven't collected any
                # geometries, return the units here
                if level == base_geolevel:
                    return units

            # only query geolevels below (smaller in size, after the 
            # primary search geolevel) the geolevel parameter
            elif searching:
                # union the selected geometries
                if len(units) == 0:
                    union = None
                else:
                    # this always rebuilds the current extent of all the
                    # selected geounits
                    geoms = GeometryCollection(map(lambda unit:unit.geom, units), srid=units[0].geom.srid)
                    union = enforce_multi(geoms, collapse=True)

                # set or merge this onto the existing selection
                if union is None:
                    intersects = selection
                else:
                    intersects = selection.difference(union)

                if inside:
                    # the remainder geometry is the intersection of the 
                    # district and the difference of the selected geounits
                    # and the current extent
                    try:
                        remainder = boundary.intersection(intersects)
                    except GEOSException, ex:
                        # it is not clear what this means
                        remainder = empty_geom(boundary.srid)
                else:
                    # the remainder geometry is the geounit selection 
                    # differenced with the boundary (leaving the 
                    # selection that lies outside the boundary) 
                    # differenced with the intersection (the selection
                    # outside the boundary and outside the accumulated
                    # geometry)
                    try:
                        remainder = selection.difference(boundary)

                        remainder = remainder.intersection(intersects)
                    except GEOSException, ex:
                        # it is not clear what this means, or why it happens
                        remainder = empty_geom(boundary.srid)

                remainder = enforce_multi(remainder)

                # Check if the remainder is empty -- it may have been 
                # converted, or errored out above, in which case we just
                # have to move on.
                if not remainder.empty:
                    query = "SELECT id,child_id,geolevel_id,st_ashexewkb(geom,'NDR') FROM redistricting_geounit WHERE geolevel_id = %d AND " % level.id

                    if level == base_geolevel:
                        # Query by center
                        query += "st_intersects(center, geomfromewkt('%s'))" % remainder.ewkt
                    else:
                        # Query by geom
                        query += "st_within(geom, geomfromewkt('%s'))" % remainder.ewkt

                    # Execute our custom SQL
                    cursor = connection.cursor()
                    cursor.execute(query)
                    rows = cursor.fetchall()
                    count = 0
                    for row in rows:
                        count += 1
                        geom = GEOSGeometry(row[3])
                        units.append(Geounit(id=row[0],geom=geom,child_id=row[1],geolevel_id=row[2]))

        # Send back the collected Geounits
        return units

    def __unicode__(self):
        """
        Represent the Geounit as a unicode string. This is the Geounit's 
        name.
        """
        return self.name

class Characteristic(models.Model):
    """
    A data value for a Geounit's Subject.

    A Characteristic is the numerical data value measured for a Geounit for
    a specific Subject. For example, this could be 1,200 for the Total 
    Population of Ada County.
    """

    # The subject that this value relates to
    subject = models.ForeignKey(Subject)
    # The Geounit that this value relates to
    geounit = models.ForeignKey(Geounit)
    # The value as a raw decimal number
    number = models.DecimalField(max_digits=12,decimal_places=4)
    # The value as a percentage of the value for this geounit of the subject given as 
    # the percentage_denominator (if any)
    percentage = models.DecimalField(max_digits=12,decimal_places=8, null=True, blank=True)

    def __unicode__(self):
        """
        Represent the Characteristic as a unicode string. The 
        Characteristic string is in the form of "Subject for Geounit: 
        Number"
        """
        return u'%s for %s: %s' % (self.subject, self.geounit, self.number)

class Target(models.Model):
    """
    A set of data values that bound the ComputedCharacteristics of a 
    District.

    A Target contains the upper and lower bounds for a Subject. When 
    editing districts, these targets are used by the symbolizers to 
    represent districts as over or under the target range.
    """

    # The subject that this target relates to
    subject = models.ForeignKey(Subject)

    # The first range value
    range1 = models.DecimalField(max_digits=12,decimal_places=4)

    # The second data value
    range2 = models.DecimalField(max_digits=12,decimal_places=4)

    # The central data value
    value = models.DecimalField(max_digits=12,decimal_places=4)

    class Meta:
        """
        Additional information about the Target model.
        """
        ordering = ['subject']

    def __unicode__(self):
        """
        Represent the Target as a unicode string. The Target string is
        in the form of "Subject : Value (Range1 - Range2)"
        """
        return u'%s : %s (%s - %s)' % (self.subject, self.value, self.range1, self.range2)

class Plan(models.Model):
    """
    A collection of Districts for an area of coverage, like a state.

    A Plan is created by a user to represent multiple Districts. A Plan
    may be a template (created by admins, copyable by all users), or shared
    (created by users, copyable by all users).  In addition, Plans are 
    versioned; the Plan version is the most recent version of all Districts
    that are a part of this Plan.
    """

    # The name of this plan
    name = models.CharField(max_length=200)

    # A description of the plan
    description = models.CharField(max_length=500, db_index=True, blank=True)

    # Is this plan a template?
    is_template = models.BooleanField(default=False)

    # Is this plan shared?
    is_shared = models.BooleanField(default=False)

    # Is this plan 'pending'? Pending plans are being constructed in the
    # backend, and should not be visible in the UI
    is_pending = models.BooleanField(default=False)

    # Is this plan considered a valid plan based on validation criteria?
    is_valid = models.BooleanField(default=False)

    # The most recent version of the districts in this plan.
    version = models.PositiveIntegerField(default=0)

    # The oldest available stored version of this plan.
    min_version = models.PositiveIntegerField(default=0)

    # The time when this Plan was created.
    created = models.DateTimeField(auto_now_add=True)

    # The time when this Plan was edited.
    edited = models.DateTimeField(auto_now=True)

    # The owner of this Plan
    owner = models.ForeignKey(User)

    # The legislative body that this plan is for
    legislative_body = models.ForeignKey(LegislativeBody)

    # A flag to indicate that upon post_save, when a plan is created,
    # it should create an Unassigned district. There are times when
    # this behaviour should be skipped (when copying plans, for example)
    create_unassigned = True

    def __unicode__(self):
        """
        Represent the Plan as a unicode string. This is the Plan's name.
        """
        return self.name

    class Meta:
        """
        Define a unique constraint on 2 fields of this model.
        """
        unique_together = ('name','owner','legislative_body',)

    
    def targets(self):
        """
        Get the targets associated with this plan by stepping back through
        the legislative body and finding distinct targets for displayed subjects
        among all the geolevels in the body. This will return a django queryset
        if successful.
        """
        try:
            levels = LegislativeLevel.objects.filter(legislative_body = self.legislative_body).values('target').distinct()
            targets = Target.objects.filter(id__in=levels, subject__is_displayed = True)
            return targets
        except Exception as ex:
            print('Unable to get targets for plan %s: %s' % (self.name, ex))
            raise ex

    def get_nth_previous_version(self, steps):
        """
        Get the version of this plan N steps away.

        Since editing a plan in its history purges higher versions of the
        districts in the plan, the version numbers become discontinuous.
        In order to support purging with these discontinuous version 
        numbers, this method assists in finding the valid version number
        of the plan that is so many steps behind the current plan.

        This problem does not occur when purging higher numbered versions 
        from a plan.

        Parameters:
            steps -- The number of 'undo' steps away from the current 
                     plan's version.

        Returns:
            A valid version of this plan in the past.
        """
        versions = self.district_set.order_by('-version').values('version').annotate(count=Count('version'))

        if steps < len(versions):
            return versions[steps]['version']

        # if the number of steps exceeds the total history of the
        # plan, the version cannot be less than zero. In addition,
        # all plans are guaranteed to have a version 0.
        return 0;


    def purge(self, before=None, after=None):
        """
        Purge portions of this plan's history.

        Use one of 'before' or 'after' keywords to purge either direction.
        If both are used, only the versions before will be purged.

        Keywords:
            before -- purge the history of this plan prior to this version.
            after -- purge the history of this plan after this version.
        """
        if before is None and after is None:
            return

        if not before is None:
            # Can't purge before zero, since that's the starting point
            if before <= 0:
                return

            ds = self.get_districts_at_version(before, include_geom=False)
            allQ = Q(plan__isnull=True)
            for d in ds:
                #maxqset = self.district_set.filter(district_id=d.district_id)
                #maxver = maxqset.aggregate(Max('version'))['version__max']

                # Filter on this district
                q1 = Q(district_id=d.district_id)

                # Filter on all previous versions
                q2 = Q(version__lt=d.version)
               
                # Accumulate the criteria
                allQ = allQ | (q1 & q2)

            # get the IDs of all the offenders
            deleteme = self.district_set.filter(allQ)
        else:
            # Purge any districts between the version provided
            # and the latest version
            deleteme = self.district_set.filter(version__gt=after)

        # since comments are loosely bound, manually remove them, too
        pks = deleteme.values_list('id',flat=True)
        pkstr = map(lambda id:str(id), pks) # some genious uses text as a pk?
        ct = ContentType.objects.get(app_label='redistricting',model='district')
        Comment.objects.filter(object_pk__in=pkstr,content_type=ct).delete()

        # since tags are loosely bound, manually remove them, too
        TaggedItem.objects.filter(object_id__in=pks,content_type=ct).delete()

        # delete all districts at once
        deleteme.delete()

        
    def purge_beyond_nth_step(self, steps):
        """
        Purge portions of this plan's history that
        are beyond N undo steps away.

        Parameters:
            steps -- The number of 'undo' steps away from the current 
                     plan's version.
        """
        if (steps >= 0):
            prever = self.get_nth_previous_version(steps)
            if prever > self.min_version:
                self.purge(before=prever)
                self.min_version = prever
                self.save();

    def update_num_members(self, district, num_members):
        """
        Create and save a new district version with the new number of values

        Parameters:
            district -- The district to modify
            num_members -- The new number of representatives for the district
        """
                
        # Clone the district to a new version, with new num_members
        district_copy = copy(district)
        district_copy.version = self.version
        district_copy.num_members = num_members
        district_copy.id = None
        district_copy.save()

        # Clone the characteristics, comments and tags to this new version
        district_copy.clone_relations_from(district)
                
    @transaction.commit_on_success
    def add_geounits(self, districtid, geounit_ids, geolevel, version, keep_old_versions=False):
        """
        Add Geounits to a District. When geounits are added to one 
        District, they are also removed from whichever district they're 
        currently in. 

        NOTE: All calls to 'simplify' use the spatial units -- the map 
        units in web mercator are meters, so simplify(tolerance=100.0) 
        simplifies geometries to 100 meters between points (-ish).

        Parameters:
            districtid -- The district_id (NOT the id) of the
                destination District.
            geounit_ids -- A list of Geounit ids that are to be added
                to the District.
            geolevel -- The Geolevel of the geounit_ids.
            version -- The version of the Plan that is being modified.
            keep_old_versions -- Optional. If true, no older versions are purged.

        Returns:
            Either 1) the number of Districts changed if adding geounits 
            to a district that already exists; 2) the name of the district
            created with the passed geounits.
        """

        # fix the district id so that it is definitely an integer
        districtid = int(districtid)

        # fix the version so that it is definitely an integer
        version = int(version)
        
        # incremental is the geometry that is changing
        incremental = Geounit.objects.filter(id__in=geounit_ids).unionagg()

        fixed = False

        # Get the districts in this plan, at the specified version.
        districts = self.get_districts_at_version(version, include_geom=True)

        # Check if the target district is locked
        if any((ds.is_locked and ds.district_id == districtid) for ds in districts):
            return False

        # Collect locked district geometries, and remove locked sections
        locked = District.objects.filter(id__in=[d.id for d in districts if d.is_locked]).collect()
        if locked:
            # GEOS topology exceptions are sometimes thrown when performing a difference
            # on complex geometries unless a buffer(0) is first performed.
            locked = locked if locked.empty else locked.buffer(0)
            incremental = incremental if locked.empty else incremental.difference(locked)

        self.purge(after=version)

        target = None

        # First, remove the aggregate values from districts that are
        # not the target, and intersect the geounits provided
        for district in districts:
            if district.district_id == districtid:
                # If the district_id is the target, save the target.
                target = district
                continue

            if district.geom is None:
                # Nothing can interact with no geometry
                continue

            if not district.geom.relate_pattern(incremental,'T********'):
                # if this district has later edits, REVERT them to
                # this version of the district
                if not district.is_latest_version():
                    # Clone the district to a new version, with a different
                    # shape
                    district_copy = copy(district)
                    district_copy.version = self.version + 1
                    district_copy.id = None
                    district_copy.save()

                    # Clone the characteristics, comments, and tags to this 
                    # new version
                    district_copy.clone_relations_from(district)

                    fixed = True

                # go onto the next district
                continue

            # compute the geounits before changing the boundary
            geounits = Geounit.get_mixed_geounits(geounit_ids, self.legislative_body, geolevel, district.geom, True)

            # Set the flag to indicate that the districts have been fixed
            if len(geounits) > 0:
                fixed = True

            # Difference the district with the selection
            # This may throw a GEOSException, in which case this function
            # will not complete successfully, and all changes will be
            # rolled back, thanks to the decorator commit_on_success
            try:
                geom = district.geom.difference(incremental)
            except GEOSException, ex:
                # Can this be logged?
                raise ex

            # Make sure the geom is a multi-polygon.
            district.geom = enforce_multi(geom)

            # Clone the district to a new version, with a different shape
            district_copy = copy(district)
            district_copy.version = self.version + 1
            district_copy.id = None
            district_copy.save() # this auto-generates a district_id

            # There is always a geometry for the district copy
            district_copy.simplify() # implicit save

            # Clone the characteristcs, comments, and tags to this new version
            district_copy.clone_relations_from(district)

            # Update the district stats
            district_copy.delta_stats(geounits,False)

        new_target = False
        if target is None:
            # create a temporary district
            try:
                name = self.legislative_body.member % districtid
            except:
                name = str(districtid)
            target = District(name=name, plan=self, district_id=districtid, version=self.version)
            target.save()
            new_target = True
                
        # If there are locked districts: augment the district boundary with the
        # boundary of the locked area, because get_mixed_geounits is getting
        # the geounits that lie outside of the provided geometry, but
        # within the boundaries of the geounit ids.
        if locked:
            if target.geom:
                bounds = target.geom.union(locked)
            else:
                bounds = locked
        else:
            bounds = target.geom

        # get the geounits before changing the target geometry
        geounits = Geounit.get_mixed_geounits(geounit_ids, self.legislative_body, geolevel, bounds, False)

        # set the fixed flag, since the target has changed
        if len(geounits) > 0:
            fixed = True

        # If there exists geometry in the target district
        if target.geom:
            # Combine the incremental (changing) geometry with the existing
            # target geometry
            # This may throw a GEOSException, in which case this function
            # will not complete successfully, and all changes will be
            # rolled back, thanks to the decorator commit_on_success
            try:
                union = target.geom.union(incremental)
                target.geom = enforce_multi(union)
            except GEOSException, ex:
                # Can this be logged?
                raise ex
        else:
            # Set the target district's geometry to the sum of the changing
            # Geounits
            target.geom = enforce_multi(incremental)

        # Clone the district to a new version, with a different shape.
        target_copy = copy(target)
        target_copy.version = self.version + 1
        target_copy.id = None

        target_copy.simplify() # implicit save happens here

        # Clone the characteristics, comments, and tags to this new version
        target_copy.clone_relations_from(target)

        # Update the district stats
        target_copy.delta_stats(geounits,True)

        # invalidate the plan, since it has been modified
        self.is_valid = False

        # save any changes to the version of this plan
        self.version += 1
        self.save()

        # purge old versions
        if settings.MAX_UNDOS_DURING_EDIT > 0 and not keep_old_versions:
            self.purge_beyond_nth_step(settings.MAX_UNDOS_DURING_EDIT)

        # purge the old target if a new one was created
        if new_target:
            District.objects.filter(id=target.id).delete()

        # Return a flag indicating any districts changed
        return fixed

    def get_biggest_geolevel(self):
        """
        A convenience method to get the "biggest" geolevel that could
        be used in this plan.  Helpful for get_mixed_geounits
        
        Returns:
            The geolevel in this plan with the minimum zoom level
        """
        leg_levels = LegislativeLevel.objects.filter(legislative_body = self.legislative_body)
        geolevel = leg_levels[0].geolevel

        for l in leg_levels:
            if l.geolevel.min_zoom < geolevel.min_zoom:
                geolevel = l.geolevel
        return geolevel

    def paste_districts(self, districts, version=None):
        """ 
        Add the districts with the given plan into the plan
        Parameters
            districts -- A list of districts to add to
                this plan
            version -- The plan version that requested the
                change.  Upon success, the plan will be one
                version greater.
        
        Returns:
            A list of the ids of the new, pasted districts
        """
    
        if version == None:
            version = self.version
        # Check to see if we have enough room to add these districts 
        # without going over MAX_DISTRICTS for the legislative_body
        current_districts = self.get_districts_at_version(version, include_geom=False)
        allowed_districts = self.legislative_body.max_districts + 1
        for d in current_districts:
            if d.district_id == 0 or not d.geom.empty:
                allowed_districts -= 1

        if allowed_districts <= 0:
            raise Exception('Tried to merge too many districts')

        # We've got room.  Add the districts.
        if version < self.version:
            self.purge(after=version)
        pasted_list = list()
        others = None
        for district in districts:
            new_district_id, others = self.paste_district(district, version=version, others=others)
            if new_district_id > 0:
                pasted_list.append(new_district_id)
        if len(pasted_list) > 0:
            self.version = version + 1
            self.save()
        return pasted_list

    # We'll use these types every time we paste.  Instantiate once in the class.
    global acceptable_intersections
    acceptable_intersections = ('Polygon', 'MultiPolygon', 'LinearRing')

    def paste_district(self, district, version=None, others=None):
        """
        Add the district with the given primary key into this plan

        Parameters:
            district -- The district to paste into the plan.
            version -- the plan version that requested the change.
                The saved districts will be one version greater.
                NB: If these districts are in a different plan, the ordering
                of the addition could have unexpected results

        Returns:
            The id of the created district
        """
        
        # Get the old districts from before this one is pasted
        if version == None:
            version = self.version
        new_version = version + 1
        if others == None:
            first_run = True
            others = self.get_districts_at_version(version, include_geom=True)
        else:
            first_run = False

        slot = None
        for d in others:
            if d.district_id != 0 and d.geom.empty:
                slot = d.district_id
                break

        biggest_geolevel = self.get_biggest_geolevel()

        # Pass this list of districts through the paste_districts chain
        edited_districts = list()

        # Save the new district to the plan to start
        newname = '' if slot == None else self.legislative_body.member % slot
        pasted = District(name=newname, plan=self, district_id = slot, geom=district.geom, simple = district.simple, version = new_version)
        pasted.save();
        if newname  == '':
            pasted.name = self.legislative_body.member % pasted.district_id
            pasted.save();
        pasted.clone_relations_from(district)
        
        # For the remaning districts in the plan,
        for existing in others:
            edited_districts.append(existing)
            # This existing district may be empty/removed
            if not existing.geom or not pasted.geom:
                continue
            # See if the pasted existing intersects any other existings
            if existing.geom.intersects(pasted.geom):
                intersection = existing.geom.intersection(pasted.geom)
                # We don't want touching districts (LineStrings in common) in our collection
                if intersection.geom_type == 'GeometryCollection':
                    intersection = filter(lambda g: g.geom_type in acceptable_intersections, intersection)
                    if len(intersection) == 0:
                        continue
                    intersection = MultiPolygon(intersection)
                elif intersection.empty == True or intersection.geom_type not in acceptable_intersections:
                    continue
                # If the target is locked, we'll update pasted instead;
                if existing.is_locked == True:
                    difference = pasted.geom.difference(existing.geom)
                    if difference.empty == True:
                        # This pasted district is consumed by others. Delete the record and return no number
                        pasted.delete()
                        return None
                    else:
                        pasted.geom = enforce_multi(difference)
                        pasted.simplify()
                    geounit_ids = map(str, Geounit.objects.filter(geom__bboverlaps=enforce_multi(intersection), geolevel=biggest_geolevel).values_list('id', flat=True))
                    geounits = Geounit.get_mixed_geounits(geounit_ids, self.legislative_body, biggest_geolevel.id, intersection, True)
                    pasted.delta_stats(geounits, False)
                else:
                    # We'll be updating the existing district and incrementing the version
                    difference = enforce_multi(existing.geom.difference(pasted.geom))
                    if first_run == True:
                        new_district = copy(existing)
                        new_district.id = None
                        new_district.save()
                        new_district.clone_relations_from(existing)
                    else:
                        new_district = existing
                    new_district.geom = difference
                    new_district.version = new_version
                    new_district.simplify()
                    new_district.save()
                    
                    # If we've edited the district, pop it on the new_district list
                    edited_districts.pop()
                    edited_districts.append(new_district)

                    geounit_ids = Geounit.objects.filter(geom__bboverlaps=intersection, geolevel=biggest_geolevel).values_list('id', flat=True)
                    geounit_ids = map(str, geounit_ids)

                    geounits = Geounit.get_mixed_geounits(geounit_ids, self.legislative_body, biggest_geolevel.id, intersection, True)
                    
                    if new_district.geom != None:
                        new_district.delta_stats(geounits, False)
                    else:
                        new_district.computedcharacteristic_set.all().delete()
        return (pasted.id, edited_districts)

    def get_wfs_districts(self,version,subject_id,extents,geolevel, district_ids=None):
        """
        Get the districts in this plan as a GeoJSON WFS response.
        
        This method behaves much like a WFS service, returning the GeoJSON 
        for each district. This manual view exists because the limitations
        of filtering and the complexity of the version query -- it is 
        impossible to use the WFS layer in Geoserver automatically.

        Parameters:
            version -- The Plan version.
            subject_id -- The Subject attributes to attach to the district.
            extent -- The map extents.
            district_ids -- Optional array of district_ids to filter by.

        Returns:
            GeoJSON describing the Plan.
        """

        # If explicitly asked for no district ids, return no features
        if district_ids == []:
            return []
        
        cursor = connection.cursor()
        query = """SELECT rd.id,
rd.district_id,
rd.name,
rd.is_locked,
lmt.version,
rd.plan_id,
rc.subject_id,
rc.number,
st_asgeojson(
    st_intersection(
        st_geometryn(rd.simple,%d),
            st_envelope(
                ('SRID=' || (select st_srid(rd.simple)) || ';LINESTRING(%f %f,%f %f)')::geometry
            )
        )
    ) as geom ,
rd.num_members
FROM redistricting_district as rd 
JOIN redistricting_computedcharacteristic as rc 
ON rd.id = rc.district_id 
JOIN (
    SELECT max(version) as version,district_id
    FROM redistricting_district 
    WHERE plan_id = %d 
    AND version <= %d 
    GROUP BY district_id) 
AS lmt 
ON rd.district_id = lmt.district_id 
WHERE rd.plan_id = %d 
AND rc.subject_id = %d 
AND lmt.version = rd.version 
AND st_intersects(
    st_geometryn(rd.simple,%d),
        st_envelope(
            ('SRID=' || (select st_srid(rd.simple)) || ';LINESTRING(%f %f,%f %f)')::geometry
        )
    )""" % (geolevel, \
                extents[0], \
                extents[1], \
                extents[2], \
                extents[3], \
                int(self.id), \
                int(version), \
                int(self.id), \
                int(subject_id), \
                geolevel, \
                extents[0], \
                extents[1], \
                extents[2], \
                extents[3], )

        exclude_unassigned = True

        # Filter by district_ids if the parameter is present
        if district_ids:
            # The 'int' conversion will throw an exception if the value isn't an integer.
            # This is desired, and will keep any harmful array values out of the query.
            query += ' AND rd.district_id in (' + ','.join(str(int(id)) for id in district_ids) + ')'
            exclude_unassigned = len(filter(lambda x: int(x) == 0, district_ids)) == 0

        # Don't return Unassigned district unless it was explicitly requested
        if exclude_unassigned:
            query += " AND NOT rd.name = 'Unassigned'"

        # Execute our custom query
        cursor.execute(query)
        rows = cursor.fetchall()
        features = []
        
        for row in rows:
            district = District.objects.get(pk=int(row[0]))
            # Maybe the most recent district is empty
            if row[8]:
                geom = json.loads( row[8] )
            else:
                geom = None
            compactness_calculator = Schwartzberg()
            compactness_calculator.compute(district=district)

            contiguity_calculator = Contiguity()
            contiguity_calculator.compute(district=district)

            name = row[2]
            num_members = int(row[9])

            # If this district contains multiple members, change the label
            label = name
            if (self.legislative_body.multi_members_allowed and (num_members > 1)):
                format = self.legislative_body.multi_district_label_format
                label = format.format(name=name, num_members=num_members)
            
            features.append({ 
                'id': row[0],
                'properties': {
                    'district_id': row[1],
                    'name': name,
                    'label': label,
                    'is_locked': row[3],
                    'version': row[4],
                    'number': float(row[7]),
                    'contiguous': contiguity_calculator.result,
                    'compactness': compactness_calculator.result,
                    'num_members': num_members
                },
                'geometry': geom
            })

        # Return a python dict, which gets serialized into geojson
        return features

    def get_districts_at_version(self, version, include_geom=False):
        """
        Get Plan Districts at a specified version.

        When a district is changed, a copy of the district is inserted
        into the database with an incremented version number. The plan version
        is also incremented.  This method returns all of the districts
        in the given plan at a particular version.

        Parameters:
            version -- The version of the Districts to fetch.
            include_geom -- Should the geometry of the district be fetched?

        Returns:
            A list of districts that exist in the plan at the version.
        """

        if include_geom:
            fields = 'd.*'
        else:
            fields = 'd.id, d.district_id, d.name, d.plan_id, d.version, d.is_locked, d.geom is not null as has_geom'

        return sorted(list(District.objects.raw('select %s from redistricting_district as d join (select max(version) as latest, district_id, plan_id from redistricting_district where plan_id = %%s and version <= %%s group by district_id, plan_id) as v on d.district_id = v.district_id and d.plan_id = v.plan_id and d.version = v.latest' % fields, [ self.id, version ])), key=lambda d: d.sortKey())

    @staticmethod
    def create_default(name,body,owner=None,template=True,is_pending=True,create_unassigned=True):
        """
        Create a default plan.

        Parameters:
            name - The name of the plan to create.
            owner - The system user that will own this plan.

        Returns:
            A new plan, owned by owner, with one district named 
            "Unassigned".
        """

        if not owner:
            # if no owner, admin gets this template
            owner = User.objects.get(username=settings.ADMINS[0][0])

        # Create a new plan. This will also create an Unassigned district
        # in the the plan.
        plan = Plan(name=name, legislative_body=body, is_template=template, version=0, owner=owner, is_pending=is_pending)
        plan.create_unassigned = create_unassigned

        try:
            plan.save()
        except Exception as ex:
            print( "Couldn't save plan: %s\n" % ex )
            return None

        return plan

    def get_base_geounits_in_geom(self, geom, threshold=100, simplified=False):
        """
        Get a list of the geounit ids of the geounits that comprise 
        this geometry at the base level.  

        Parameters:
            threshold - distance threshold used for buffer in/out optimization
            simplified - denotes whether or not the geom passed in is already simplified

        Returns:
            A list of tuples containing Geounit IDs and portable ids
            that lie within this geometry. 
        """

        if not geom:
           return list()

        # Simplify by the same distance threshold used for buffering
        # Note: the preserve topology parameter of simplify is needed here
        simple = geom if simplified else geom.simplify(threshold, True)

        # If the simplification makes the polygon empty, use the unsimplified polygon
        simple = simple if not simple.empty else geom

        # Perform two queries against the simplified district, one buffered in,
        # and one buffered out using the same distance as the simplification tolerance
        geolevel = self.legislative_body.get_base_geolevel()
        b_out = Geounit.objects.filter(geolevel=geolevel, center__within=simple.buffer(threshold))
        b_in = Geounit.objects.filter(geolevel=geolevel, center__within=simple.buffer(-1 * threshold))

        # Find the geounits that are different between the two queries,
        # and check if they are within the unsimplified district
        b_in_values_set = set(b_in.values_list('id', 'portable_id'))
        b_out_values_set = set(b_out.values_list('id', 'portable_id'))
        diff = set(b_out_values_set ^ b_in_values_set)
        diffwithin = []
        if len(diff) > 0:
            diffids = reduce(lambda x,y: x+y, list(diff))
            diffwithin = [(unit.id, unit.portable_id) for unit in Geounit.objects.filter(id__in=diffids) if unit.center.within(geom)]

        # Combine the geounits that were within the unsimplifed district with the buffered in list
        return list(b_in_values_set | set(diffwithin))

    def get_base_geounits(self, threshold=100):
        """
        Get a list of the geounit ids of the geounits that comprise 
        this plan at the base level.  

        Parameters:
            threshold - distance threshold used for buffer in/out optimization

        Returns:
            A list of tuples containing Geounit IDs, portable ids,
            district ids, and num_members that lie within this Plan. 
        """

        # Collect the geounits for each district in this plan
        geounits = []
        for district in self.get_districts_at_version(self.version, include_geom=True):
            # Unassigned is district 0
            if district.district_id > 0:
                districtunits = district.get_base_geounits(threshold)
                # Add extra district data to the tuples
                geounits.extend([(gid, pid, district.district_id, district.num_members) for (gid, pid) in districtunits])
        
        return geounits

    def get_assigned_geounits(self, threshold=100, version=None):
        """
        Get a list of the geounit ids of the geounits that comprise 
        this plan at the base level. This is different than
        get_base_geounits, because it doesn't return district ids
        along with the geounit ids, and should therefore be more performant.

        Parameters:
            threshold - distance threshold used for buffer in/out optimization
            version -- The version of the Plan.

        Returns:
            A list of tuples containing Geounit IDs and portable ids
            that lie within this Plan. 
        """

        if version == None:
           version = self.version

        # TODO: enhance performance. Tried various ways to speed this up by
        # creating a union of simplified geometries and passing it to get_base_geounits.
        # This seems like it would be faster, since the amount of query overhead is
        # reduced, but this offered no performance improvement, and instead caused
        # some accuracty issues. This needs further examination.
        geounits = []
        for district in self.get_districts_at_version(version,include_geom=True):
            if district.district_id > 0:
                geounits.extend(district.get_base_geounits(threshold))
        
        return geounits

    def get_unassigned_geounits(self, threshold=100, version=None):
        """
        Get a list of the geounit ids of the geounits that do not belong to
        any district of this plan at the base level. 

        Parameters:
            threshold - distance threshold used for buffer in/out optimization
            version -- The version of the Plan.

        Returns:
            A list of tuples containing Geounit IDs and portable ids
            that do not belong to any districts within this Plan. 
        """

        # The unassigned district contains all the unassigned items.
        if version:
            unassigned = self.district_set.filter(district_id=0, version__lte=version).order_by('-version')[0]
        else:
            unassigned = self.district_set.filter(district_id=0).order_by('-version')[0]

        # Return the base geounits of the unassigned district
        return unassigned.get_base_geounits(threshold)

    def get_available_districts(self, version=None):
        """
        Get the number of districts that are available in the current plan.

        Returns:
            The number of districts that may added to this plan.
        """
        if version == None:
           version = self.version
        current_districts = self.get_districts_at_version(version, include_geom=False)
        available_districts = self.legislative_body.max_districts
        for d in current_districts:
            if d.has_geom and not d.geom.empty and d.district_id > 0:
                available_districts -= 1

        return available_districts

    def fix_unassigned(self, version=None, threshold=100):
        """
        Assign unassigned base geounits that are fully contained within
        or adjacent to another district

        First fix any unassigned geounits that are fully contained within a district.
        Only fix other adjacent geounits if the minimum percentage of assigned
        geounits has been reached.

        Parameters:
            version -- The version of the Plan that is being fixed.
            threshold - distance threshold used for buffer in/out optimization

        Returns:
            Whether or not the fix was successful, and a message
        """

        if version == None:
           version = self.version

        num_unassigned = 0
        geolevel = self.legislative_body.get_base_geolevel()

        # Check that there are unassigned geounits to fix
        unassigned_district = self.district_set.get(district_id=0, version=version)
        unassigned_geom = unassigned_district.geom
        if not unassigned_geom or unassigned_geom.empty:
            return False, 'There are no unassigned units that can be fixed.'

        # Get the unlocked districts in this plan with geometries
        districts = self.get_districts_at_version(version, include_geom=False)
        districts = District.objects.filter(id__in=[d.id for d in districts if d.district_id != 0 and not d.is_locked])
        districts = [d for d in districts if d.geom and not d.geom.empty]

        # Storage for geounits that need to be added. Map of tuples: geounitid -> (district_id, dist_val)
        to_add = {}

        # Check if any unassigned clusters are within the exterior of a district
        for unassigned_poly in unassigned_geom:
            for district in districts:
                for poly in district.geom:
                    if unassigned_poly.within(Polygon(poly.exterior_ring)):
                        for tup in self.get_base_geounits_in_geom(unassigned_poly, threshold=threshold):
                            to_add[tup[0]] = (district.district_id, 0)

        # Check if all districts have been assigned
        num_districts = len(self.get_districts_at_version(version, include_geom=False)) - 1
        not_all_districts_assigned = num_districts < self.legislative_body.max_districts
        if not_all_districts_assigned and not to_add:
            return False, 'All districts need to be assigned before fixing can occur. Currently: ' + str(num_districts)

        # Only check for adjacent geounits if all districts are assigned
        if not not_all_districts_assigned:
            # Get unassigned geounits, and subtract out any that have been added to to_add
            unassigned = self.get_unassigned_geounits(threshold=threshold, version=version)
            unassigned = [t[0] for t in unassigned]
            num_unassigned = len(unassigned)
            unassigned = list(set(unassigned) - set(to_add.keys()))
    
            # Check that the percentage of assigned base geounits meets the requirements
            num_total_units = Geounit.objects.filter(geolevel=geolevel).count()
            pct_unassigned = 1.0 * num_unassigned / num_total_units
            pct_assigned = 1 - pct_unassigned
            min_pct = settings.FIX_UNASSIGNED_MIN_PERCENT / 100.0
            below_min_pct = pct_assigned < min_pct
            if below_min_pct and not to_add:
                return False, 'The percentage of assigned units is: ' + str(int(pct_assigned * 100)) + '. Fixing unassigned requires a minimum percentage of: ' + str(settings.FIX_UNASSIGNED_MIN_PERCENT)
    
            if not below_min_pct:
                # Get the unassigned geounits from the ids
                unassigned = list(Geounit.objects.filter(pk__in=unassigned))
    
                # Remove any unassigned geounits that aren't on the edge
                temp = []
                for poly in unassigned_geom:
                    exterior = Polygon(poly.exterior_ring)
                    for g in unassigned:
                        if not g in temp and g.geom.intersects(unassigned_geom):
                            temp.append(g)
                unassigned = temp
                
                # Set up calculator/storage for comparator values (most likely population)
                # Sum is not imported, because of conflicts with the 'models' Sum
                ns = 'publicmapping.redistricting.calculators'
                sum = getattr(getattr(getattr(__import__(ns), 'redistricting'), 'calculators'), 'Sum')        
                calculator = sum()
                calculator.arg_dict['value1'] = ('subject', settings.FIX_UNASSIGNED_COMPARATOR_SUBJECT)
        
                # Test each unassigned geounit with each unlocked district to see if it should be assigned
                for district in districts:
                    # Calculate the comparator value for the district
                    calculator.compute(district=district)
                    dist_val = calculator.result
        
                    # Check if geounits are touching the district
                    for poly in district.geom:
                        exterior = Polygon(poly.exterior_ring)
                        for geounit in unassigned:
                            if geounit.geom.touches(exterior):
                                if (geounit.id not in to_add or dist_val < to_add[geounit.id][1]):
                                    to_add[geounit.id] = (district.district_id, dist_val)

        # Add all geounits that need to be fixed
        if to_add:
            # Compile lists of geounits to add per district
            district_units = {}
            for gid, tup in to_add.items():
                did = tup[0]
                if did in district_units:
                    units = district_units[did]
                else:
                    units = []
                    district_units[did] = units
                units.append(gid)

            # Add units for each district, and update version, since it changes when adding geounits
            for did, units in district_units.items():
                self.add_geounits(did, [str(p) for p in units], geolevel, version, True)
                version = self.version
                
            # Fix versions so a single undo can undo the entire set of fixes
            num_adds = len(district_units.items())
            if num_adds > 1:
                # Delete interim unassigned districts
                self.district_set.filter(district_id=0, version__in=range(self.version - num_adds + 1, self.version)).delete()

                # Set all changed districts to the current version
                for dist in self.district_set.filter(version__in=range(self.version - num_adds + 1, self.version)):
                    dist.version = self.version
                    dist.save()

            # Return status message
            num_fixed = len(to_add)
            text = 'Number of units fixed: ' + str(num_fixed)
            num_remaining = num_unassigned - num_fixed
            if (num_remaining > 0):
                text += ', Number of units remaining: ' + str(num_remaining)
            return True, text

        return False, 'No unassigned units could be fixed. Ensure the appropriate districts are not locked.'

    @transaction.commit_manually
    def combine_districts(self, target, components, version=None):
        """
        Given a target district, add the components and combine
        their scores and geography.  Target and components should
        be districts within this plan
        Parameters:
            target - A district within this plan
            components - A list of districts within this plan
                to combine with the target
            
        Returns:
            Whether the operation was successful
        """
        # Check to be sure they're all in the same version and don't 
        # overlap - that should never happen
        if version == None:
            version = self.version
        if version != self.version:
            self.purge(after=version)

        district_keys = set(map(lambda d: d.id, components))
        district_keys.add(target.id)

        district_version = self.get_districts_at_version(version)
        version_keys = set(map(lambda d: d.id, district_version))
        if not district_keys.issubset(version_keys):
            raise Exception('Attempted to combine districts not in the same plan or version') 
        if target.is_locked:
            raise Exception('You cannot combine with a locked district')

        try:
            target.id = None
            target.version = version + 1
            target.save()

            # Combine the stats for all of the districts
            all_characteristics = ComputedCharacteristic.objects.filter(district__in=district_keys)
            all_subjects = Subject.objects.order_by('-percentage_denominator').all()
            for subject in all_subjects:
                relevant_characteristics = filter(lambda c: c.subject == subject, all_characteristics)
                number = sum(map(lambda c: c.number, relevant_characteristics))
                percentage = Decimal('0000.00000000')
                if subject.percentage_denominator:
                    denominator = ComputedCharacteristic.objects.get(subject=subject.percentage_denominator,district=target)
                    if denominator:
                        if denominator.number > 0:
                            percentage = number / denominator.number
                cc = ComputedCharacteristic(district=target, subject=subject, number=number, percentage=percentage)
                cc.save()

            # Create a new copy of the target geometry
            all_geometry = map(lambda d: d.geom, components)
            all_geometry.append(target.geom)
            target.geom = enforce_multi(GeometryCollection(all_geometry,srid=target.geom.srid), collapse=True)
            target.simplify()

            # Eliminate the component districts from the version
            for component in components:
                if component.district_id == target.district_id:
                    # Pasting a district to itself would've been handled earlier
                    continue
                component.id = None
                component.geom = MultiPolygon([], srid=component.geom.srid)
                component.version = version + 1
                component.simplify() # implicit save

            self.version += 1
            self.save()
            transaction.commit()
            return True, self.version
        except Exception as ex:
            transaction.rollback()
            return False

class PlanForm(ModelForm):
    """
    A form for displaying and editing a Plan.
    """
    class Meta:
        """
        A helper class that describes the PlanForm.
        """

        # This form's model is a Plan
        model=Plan
    

class District(models.Model):
    """
    A collection of Geounits, aggregated together.

    A District is a part of a Plan, and is composed of many Geounits. 
    Districts have geometry, simplified geometry, and pre-computed data
    values for Characteristics.
    """

    class Meta:
        """
        A helper class that describes the District class.
        """

        # Order districts by name, by default.
        ordering = ['name']

    # The district_id of the district, this is not the primary key ID,
    # but rather, an ID of the district that remains constant over all
    # versions of the district.
    district_id = models.PositiveIntegerField(default=None)

    # The name of the district
    name = models.CharField(max_length=200)

    # The parent Plan that contains this District
    plan = models.ForeignKey(Plan)

    # The geometry of this district (high detail)
    geom = models.MultiPolygonField(srid=3785, blank=True, null=True)

    # The simplified geometry of this district
    simple = models.GeometryCollectionField(srid=3785, blank=True, null=True)

    # The version of this district.
    version = models.PositiveIntegerField(default=0)

    # A flag that indicates if this district should be edited
    is_locked = models.BooleanField(default=False)

    # The number of representatives configured for this district
    num_members = models.PositiveIntegerField(default=1)

    # This is a geographic model, so use the geomanager for objects
    objects = models.GeoManager()
    
    def sortKey(self):
        """
        Sort districts by name, with numbered districts first.

        Returns:
            The Districts, sorted in numerical order.
        """
        name = self.name;
        prefix = self.plan.legislative_body.member
        index = prefix.find('%')
        if index >= 0:
            prefix = prefix[0:index]
        else:
            index = 0

        if name.startswith(prefix):
            name = name[index:]
        if name.isdigit():
            return '%03d' % int(name)
        return name 

    def sortVer(self):
        """
        Sort a list of districts first by district_id, then by 
        version number.

        Returns:
            district_id * 1000 + self.version
        """
        return self.district_id * 10000 + self.version

    def is_latest_version(self):
        """
        Determine if this district is the latest version of the district
        stored. If a district is not assigned to a plan, it is always 
        considered the latest version.
        """
        if self.plan:
            qset = self.plan.district_set.filter(district_id=self.district_id)
            maxver = qset.aggregate(Max('version'))['version__max']

            return self.version == maxver
        return true

    def __unicode__(self):
        """
        Represent the District as a unicode string. This is the District's 
        name.
        """
        return self.name

    def delta_stats(self,geounits,combine):
        """
        Update the stats for this district incrementally. This method
        iterates over all the computed characteristics and adds or removes
        the characteristic values for the specific geounits only.

        Parameters:
            geounits -- The Geounits to add or remove to this districts
                ComputedCharacteristic value.
            combine -- The aggregate value computed should be added or
                removed from the ComputedCharacteristicValue

        Returns:
            True if the stats for this district have changed.
        """
        # Get the subjects that don't rely on others first - that will save us
        # from computing characteristics for denominators twice
        all_subjects = Subject.objects.order_by('-percentage_denominator').all()
        changed = False

        # For all subjects
        for subject in all_subjects:
            # Aggregate all Geounits Characteristic values
            aggregate = Characteristic.objects.filter(geounit__in=geounits, subject__exact=subject).aggregate(Sum('number'))['number__sum']
            # If there are aggregate values for the subject and geounits.
            if not aggregate is None:
                # Get the pre-computed values
                defaults = {'number':Decimal('0000.00000000')}
                computed,created = ComputedCharacteristic.objects.get_or_create(subject=subject,district=self,defaults=defaults)

                if combine:
                    # Add the aggregate to the computed value
                    computed.number += aggregate
                else:
                    # Subtract the aggregate from the computed value
                    computed.number -= aggregate

                # If this subject is viewable as a percentage, do the math
                # using the already calculated value for the denominator
                if subject.percentage_denominator:
                    denominator = ComputedCharacteristic.objects.get(subject=subject.percentage_denominator,district=self)
                    if denominator:
                        if denominator.number > 0:
                            computed.percentage = computed.number / denominator.number
                        else:
                            computed.percentage = '0000.00000000'

                # If there are aggregate values for the subject & geounits.
                computed.save();

                changed = True

        return changed

    def reset_stats(self):
        """
        Reset the statistics to zero for this district. This method walks
        through all available subjects, and sets the computed 
        characteristic for this district to zero.

        Returns:
            True if the district stats were changed.
        """
        all_subjects = Subject.objects.all()
        changed = False

        # For all subjects
        for subject in all_subjects:
            # Get the pre-computed values
            defaults = {'number':Decimal('0000.00000000')}
            computed,created = ComputedCharacteristic.objects.get_or_create(subject=subject,district=self,defaults=defaults)

            if not created:
                # Add the aggregate to the computed value
                computed.number = '0000.00000000'
                computed.percentage = '0000.00000000'

                # Save these values
                computed.save();

                changed = True

        return changed


    def clone_relations_from(self, origin):
        """
        Copy the computed characteristics, comments, and tags from one 
        district to another.

        Cloning District Characteristics, Comments and Tags are required when 
        cloning, copying, or instantiating a template district.

        Parameters:
            origin -- The source District.
        """
        cc = ComputedCharacteristic.objects.filter(district=origin)
        for c in cc:
            c.id = None
            c.district = self
            c.save()

        ct = ContentType.objects.get(app_label='redistricting', model='district')
        cmts = Comment.objects.filter(object_pk=origin.id, content_type=ct)
        for cmt in cmts:
            cmt.id = None
            cmt.object_pk = self.id
            cmt.save()

        items = TaggedItem.objects.filter(object_id=origin.id, content_type=ct)
        for item in items:
            item.id = None
            item.object_id = self.id
            item.save()


    def get_base_geounits(self, threshold=100):
        """
        Get a list of the geounit ids of the geounits that comprise 
        this district at the base level.  
        
        We'll check this by seeing whether the centroid of each geounits 
        fits within the simplified geometry of this district.

        Parameters:
            threshold - distance threshold used for buffer in/out optimization

        Returns:
            A list of tuples containing Geounit IDs, portable ids, and num_members
            that lie within this District. 
        """
        return self.plan.get_base_geounits_in_geom(self.geom, threshold);

    def get_contiguity_overrides(self):
        """
        Retrieve any contiguity overrides that are applicable
        to this district. This is defined by any ContiguityOverride
        objects whose two referenced geounits both fall within
        the geometry of this district.
        """
        if not self.geom:
            return []

        filter = Q(override_geounit__geom__within=self.geom)
        filter = filter & Q(connect_to_geounit__geom__within=self.geom)
        return list(ContiguityOverride.objects.filter(filter))
    
    def simplify(self):
        """
        Simplify the geometry into a geometry collection in the simple 
        field.

        Parameters:
            self - The district
        """
        plan = self.plan
        body = plan.legislative_body
        # This method returns the geolevels from largest to smallest
        # but we want them the other direction
        levels = body.get_geolevels()
        levels.reverse()

        if not self.geom is None:
            simples = []
            index = 1
            for level in levels:
                while index < level.id:
                    # We want to store the levels within a GeometryCollection, and make it so the level id
                    # can be used as the index for lookups. So for disparate level ids, empty geometries need
                    # to be stored. Empty GeometryCollections cannot be inserted into a GeometryCollection,
                    # so a Point at the origin is used instead.
                    simples.append(Point((0,0), srid=self.geom.srid))
                    index += 1
                if self.geom.num_coords > 0:
                    simple = self.geom.simplify(preserve_topology=True,tolerance=level.tolerance)
                    if not simple.valid:
                        simple = simple.buffer(0)
                    simples.append(simple)
                else:
                    simples.append( self.geom )
                index += 1
            self.simple = GeometryCollection(tuple(simples),srid=self.geom.srid)
            self.save()
        else:
            self.simple = None
            self.save()

# Enable tagging of districts by registering them with the tagging module
tagging.register(District)


class ComputedCharacteristic(models.Model):
    """
    ComputedCharacteristics are cached, aggregate values of Characteristics
    for Districts.

    ComputedCharacteristics represent the sum of the Characteristic values
    for all Geounits in a District. There will be one 
    ComputedCharacteristic per District per Subject.
    """

    # The subject
    subject = models.ForeignKey(Subject)

    # The district and area
    district = models.ForeignKey(District)

    # The total aggregate as a raw value
    number = models.DecimalField(max_digits=12,decimal_places=4)

    # The aggregate as a percentage of the percentage_denominator's aggregated value.
    percentage = models.DecimalField(max_digits=12, decimal_places=8, null=True, blank=True)

    class Meta:
        """
        A helper class that describes the ComputedCharacteristic class.
        """
        ordering = ['subject']


class Profile(models.Model):
    """
    Extra user information that doesn't fit in Django's default user
    table.

    Profiles for The Public Mapping Project include a password hint,
    and an organization name.
    """
    user = models.OneToOneField(User)

    # A user's organization
    organization = models.CharField(max_length=256)

    # A user's password hint.
    pass_hint = models.CharField(max_length=256)

    def __unicode__(self):
        """
        Represent the Profile as a unicode string. This is the a string
        with the User's name.
        """
        return "%s's profile" % self.user.username


def update_profile(sender, **kwargs):
    """
    A trigger that creates profiles when a user is saved.
    """
    created = kwargs['created']
    user = kwargs['instance']
    if created:
        profile = Profile(user=user, organization='', pass_hint='')
        profile.save()

def set_district_id(sender, **kwargs):
    """
    Incremented the district_id (NOT the primary key id) when a district
    is saved. The district_id is unique to the plan/version.  The 
    district_id may already be set, but this method ensures that it is set
    when saved.
    """
    district = kwargs['instance']
    if district.district_id is None:
        districts = district.plan.get_districts_at_version(district.version, include_geom=False)
        ids_in_use = map(lambda d: d.district_id, filter(lambda d: True if d.has_geom else False, districts))
        max_districts = district.plan.legislative_body.max_districts + 1
        if len(ids_in_use) >= max_districts:
            raise ValidationError("Plan is at maximum district capacity of %d" % max_districts)
        else:
            # Find one not in use - 0 is unassigned
            # TODO - update this if unassigned is not district_id 0
            for i in range(1, max_districts+1):
                if i not in ids_in_use:
                    district.district_id = i
                    return

def update_plan_edited_time(sender, **kwargs):
    """
    Update the time that the plan was edited whenever the plan is saved.
    """
    district = kwargs['instance']
    plan = district.plan;
    plan.edited = datetime.now()
    plan.save()

def create_unassigned_district(sender, **kwargs):
    """
    When a new plan is saved, all geounits must be inserted into the 
    Unassigned districts.
    """
    plan = kwargs['instance']
    created = kwargs['created']

    if created and plan.create_unassigned:
        plan.create_unassigned = False

        unassigned = District(name="Unassigned", version = 0, plan = plan, district_id=0)

        biggest_geolevel = plan.get_biggest_geolevel()
        all_geom = Geounit.objects.filter(geolevel=biggest_geolevel).collect()

        if plan.district_set.count() > 0:
            taken = plan.district_set.all().collect()
            unassigned.geom =  enforce_multi(all_geom.difference(taken))
            unassigned.simplify() # implicit save
            geounit_ids = map(str, Geounit.objects.filter(geom__bboverlaps=unassigned.geom, geolevel=biggest_geolevel).values_list('id', flat=True))
            geounits = Geounit.get_mixed_geounits(geounit_ids, plan.legislative_body, biggest_geolevel.id, unassigned.geom, True)
        else:
            unassigned.geom = enforce_multi(all_geom)
            unassigned.simplify() #implicit save
            geounits = Geounit.objects.filter(geolevel=biggest_geolevel)

        unassigned.delta_stats(geounits, True)

        
# Connect the post_save signal from a User object to the update_profile
# helper method
post_save.connect(update_profile, sender=User, dispatch_uid="publicmapping.redistricting.User")
# Connect the pre_save signal to the set_district_id helper method
pre_save.connect(set_district_id, sender=District)
# Connect the post_save signal to the update_plan_edited_time helper method
post_save.connect(update_plan_edited_time, sender=District)
# Connect the post_save signal from a Plan object to the 
# create_unassigned_district helper method (don't remove the dispatch_uid or 
# this signal is sent twice)
post_save.connect(create_unassigned_district, sender=Plan, dispatch_uid="publicmapping.redistricting.Plan")

def can_edit(user, plan):
    """
    Can a user edit a plan?
    
    In order to edit a plan, Users must own it or be a staff member.  
    Templates cannot be edited, only copied.

    Parameters:
        user -- A User
        plan -- A Plan

    Returns:
        True if the User has permissions to edit the Plan.

    """
    return (plan.owner == user or user.is_staff) and not plan.is_template and not plan.is_shared

def can_view(user, plan):
    """
    Can a user view a plan?

    In order to view a plan, the plan must have the shared flag set.

    Parameters:
        user -- A User
        plan -- A Plan

    Returns:
        True if the User has permissions to view the Plan.
    """
    return plan.is_shared or plan.is_template


def can_copy(user, plan):
    """
    Can a user copy a plan?

    In order to copy a plan, the user must be the owner, or a staff 
    member to copy a plan they own.  Any registered user can copy a 
    template.

    Parameters:
        user -- A User
        plan -- A Plan

    Returns:
        True if the User has permission to copy the Plan.
    """
    return plan.is_template or plan.is_shared or plan.owner == user or user.is_staff

def empty_geom(srid):
    """
    Create an empty MultiPolygon.

    Parameters:
        srid -- The spatial reference for this empty geometry.

    Returns:
        An empty geometry.
    """
    return MultiPolygon([], srid=srid)

def enforce_multi(geom, collapse=False):
    """
    Make a geometry a multi-polygon geometry.

    This method wraps Polygons in MultiPolygons. If geometry exists, but is
    neither polygon or multipolygon, an empty geometry is returned. If no
    geometry is provided, no geometry (None) is returned.

    Parameters:
        geom -- The geometry to check/enforce.
        collapse -- A flag indicating that this method should collapse 
                    the resulting multipolygon via cascaded_union. With
                    this flag, the method still returns a multipolygon.
    Returns:
        A multi-polygon from any geometry type.
    """
    mpoly = MultiPolygon([])
    if not geom is None:
        mpoly.srid = geom.srid

        if geom.empty:
            pass
        elif geom.geom_type == 'MultiPolygon':
            if collapse:
                mpoly = enforce_multi( geom.cascaded_union )
            else:
                mpoly = geom
        elif geom.geom_type == 'Polygon':
            # Collapse has no meaning if this is a single polygon
            mpoly.append(geom)
        elif geom.geom_type == 'GeometryCollection':
            components = []
            for item in geom:
                for component in enforce_multi(item):
                    mpoly.append(component)

            if collapse:
                # Collapse the multipolygon group
                mpoly = enforce_multi( mpoly, collapse )

    return mpoly

class ScoreFunction(models.Model):
    """
    Score calculation definition
    """

    # Namepace of the calculator module to use for scoring
    calculator = models.CharField(max_length=500)

    # Name of this score function
    name = models.CharField(max_length=50)

    # Label to be displayed for scores calculated with this funciton
    label = models.CharField(max_length=100, blank=True)

    # Description of this score function
    description = models.TextField(blank=True)

    # Whether or not this score function is for a plan
    is_planscore = models.BooleanField(default=False)

    # Whether a user can select this function for use in a 
    # statistics set
    is_user_selectable = models.BooleanField(default=False)
    
    class Meta:
        """
        Additional information about the Subject model.
        """

        # The default method of sorting Subjects should be by 'sort_key'
        ordering = ['name']

    def get_calculator(self):
        """
        Retrieve a calculator instance by name.

        Parameters:
            name -- The fully qualified name of the calculator class.

        Returns:
            An instance of the requested calculator.
        """
        parts = self.calculator.split('.')
        module = ".".join(parts[:-1])
        m = __import__( module )
        for comp in parts[1:]:
            m = getattr(m, comp)            
        return m()

    def score(self, districts_or_plans, format='raw', version=None):
        """
        Calculate the score for the object or list of objects passed in.

        Parameters:
            districts_or_plans -- Either a single district, a single plan,
                a list of districts, or a list of plans. Whether or not 
                this deals with districts or plans must be in sync with 
                the value of is_planscore.
            format -- One of 'raw', 'html', or 'json'.
                Determines how the results should be returned.

        Returns:
            A score for each district or plan contained within 
            districts_or_plans. If districts_or_plans is a single 
            district or plan, a single result will be returned. If it 
            is a list, a list of results in the same order as
            districts_or_plans will be returned.
        """
        # Raises an ImportError if there is no calculator with the given name
        calc = self.get_calculator()

        # Is districts_or_plans a list, or a single district/plan?
        is_list = isinstance(districts_or_plans, list)

        # Calculate results for every item in the list
        results = []
        for dp in (districts_or_plans if is_list else [districts_or_plans]):
            # Add all arguments that are defined for this score function
            args = ScoreArgument.objects.filter(function=self)
            arg_lst = []
            for arg in args:
                # For 'score' types, calculate the score, and then pass the result on
                if (arg.type != 'score'):
                    calc.arg_dict[arg.argument] = (arg.type, arg.value)
                else:
                    score_fn = ScoreFunction.objects.get(name=arg.value)

                    # If this is a plan score and the argument is a 
                    # district score, extract the districts from the 
                    # plan, score each individually, # and pass into the 
                    # score function as a list
                    if not (self.is_planscore and not score_fn.is_planscore):
                        calc.arg_dict[arg.argument] = ('literal', score_fn.score(dp, format=format, version=version))
                    else:
                        version = plan.version if version is None else version
                        for d in dp.get_districts_at_version(version):
                            arg_lst.append(score_fn.score(d, format=format, version=version))

            # Build the keyword arguments based on whether this is for districts, plans, or list
            if len(arg_lst) > 0:
                kwargs = { 'list': arg_lst }
            elif self.is_planscore:
                kwargs = { 'plan': dp, 'version': version or dp.version }
            else:
                kwargs = { 'district': dp }

            # Ask the calculator instance to compute the result
            calc.compute(**kwargs)

            # Format the result
            fl = format.lower()
            r = calc.html() if fl == 'html' else (calc.json() if fl == 'json' else calc.result)
            results.append(r)

        return results if is_list else results[0]

    def __unicode__(self):
        """
        Get a unicode representation of this object. This is the 
        ScoreFunction's name.
        """
        return self.name


class ScoreArgument(models.Model):
    """
    Defines the arguments passed into a score function
    """

    # The score function this argument is for
    function = models.ForeignKey(ScoreFunction)

    # The name of the argument of the score function
    argument = models.CharField(max_length=50)

    # The value of the argument to be passed
    value = models.CharField(max_length=50)

    # The type of the argument (literal, score, subject)
    type = models.CharField(max_length=10)

    def __unicode__(self):
        """
        Get a unicode representation of this object. This is the Argument's
        arg/value/type.
        """
        return "%s / %s / %s" % (self.argument, self.type, self.value)

class ScoreDisplay(models.Model):
    """
    Container for displaying score panels
    """

    # The title of the score display
    title = models.CharField(max_length=50)

    # The legislative body that this score display is for
    legislative_body = models.ForeignKey(LegislativeBody)

    # Whether or not this score display belongs on the leaderboard page
    is_page = models.BooleanField(default=False)

    # The style to be assigned to this score display
    cssclass = models.CharField(max_length=50, blank=True)

    # The owner of this ScoreDisplay
    owner = models.ForeignKey(User)

    class Meta:
        """
        Define a unique constraint on 2 fields of this model.
        """
        unique_together = ('title','owner','legislative_body')

    def __unicode__(self):
        """
        Get a unicode representation of this object. This is the Display's
        title.
        """
        return self.title

    def copy_from(self, display=None, functions=[], owner=None, title=None):
        """ 
        Given a scoredisplay and a list of functions, this method
        will copy the display and assign the copy to the new owner
        
        Parameters:
            display -- a ScoreDisplay to copy - the current 
               Demographics display 
            functions -- a list of ScoreFunctions or the primary
                keys of ScoreFunctions to replace in the display's
                first "district" ScorePanel
            owner -- the owner of the new ScoreDisplay - only set if we're not copying self
            title -- the title of the new scorefunction - only set if we're not copying self
        
        Returns:
            The new ScoreDisplay
        """
        
        if display == None:
            return

        if self != display:
            self = copy(display)
            self.id = None

            self.owner = owner if owner != None else display.owner

            # We can't have duplicate titles per owner so append "copy" if we must
            if self.owner == display.owner:
                self.title = title if title != None else "%s copy" % display.title
            else:
                self.title = title if title != None else display.title

            self.save()
            self.scorepanel_set = display.scorepanel_set.all()

        else:
            self = display


        try:
            public_demo = self.scorepanel_set.get(type='district')
            if self != display:
                self.scorepanel_set.remove(public_demo)
                demo_panel = copy(public_demo)
                demo_panel.id = None
                demo_panel.save()
                self.scorepanel_set.add(demo_panel)
            else:
                demo_panel = public_demo

            demo_panel.score_functions.clear()
            if len(functions) == 0:
                return self
            for function in functions:
                if isinstance(function, types.IntType):
                    function = ScoreFunction.objects.get(pk=function) 
                if isinstance(function, types.StringTypes):
                    function = ScoreFunction.objects.get(pk=int(function))
                if type(function) == ScoreFunction:
                    demo_panel.score_functions.add(function)
            demo_panel.save()
            self.scorepanel_set.add(demo_panel)
            self.save()
        except:
            sys.stderr.write('Failed to copy ScoreDisplay %s to %s: %s\n' % (display.title, self.title, traceback.format_exc()))

        return self

    def render(self, dorp, context=None, version=None):
        """
        Generate the markup for all the panels attached to this display.

        If the is_page property is set, render expects to receive a list
        of valid plans.

        If the is_page property is not set, render expects to receive a
        single plan, or a list of districts.

        Parameters:
            dorp -- A list of districts, plan, or list of plans.
            context -- Optional object that can be used for advanced rendering
            version -- Optional; the version of the plan or district to render.

        Returns:
            The markup for this display.
        """
        is_list = isinstance(dorp, list)

        if self.is_page and \
            (is_list and \
                any(not isinstance(item,Plan) for item in dorp)):
            # If this display is a page, it should receive a list of plans
            return ''
        elif not self.is_page:
            if is_list and \
                any(not isinstance(item,District) for item in dorp):
                # If this display is not a page, the list should be a set
                # of districts.
                return ''
            elif not is_list and \
                not isinstance(dorp,Plan):
                # If this display is not a page, the item should be a plan.
                return ''

        panels = self.scorepanel_set.all().order_by('position')

        markup = ''
        for panel in panels:
            markup += panel.render(dorp, context, version)

        return markup


class ScorePanel(models.Model):
    """
    Container for displaying multiple scores of a given type
    """

    # The type of the score display (plan, plan summary, district)
    type = models.CharField(max_length=20)

    # The score display this panel belongs to
    displays = models.ManyToManyField(ScoreDisplay)

    # Where this panel belongs within a score display
    position = models.PositiveIntegerField(default=0)
    
    # The title of the score panel
    title = models.CharField(max_length=50)
    
    # The filename of the template to be used for formatting this panel
    template = models.CharField(max_length=500)

    # The style to be assigned to this score display
    cssclass = models.CharField(max_length=50, blank=True)

    # The method of sorting the scores in this panel
    is_ascending = models.BooleanField(default=True)

    # The functions associated with this panel
    score_functions = models.ManyToManyField(ScoreFunction)

    def __unicode__(self):
        """
        Get a unicode representation of this object. This is the Panel's
        title.
        """
        return self.title

    def render(self,dorp,context=None,version=None):
        """
        Generate the scores for all the functions attached to this panel,
        and render them in the template.
        
        Only plan type panels are affected by the sorting order.

        Parameters:
            dorp -- A district, list of districts, plan, or list of plans.
            context -- Optional object that can be used for advanced rendering
            version -- Optional; version of the plan or district to render.

        Returns:
            A rendered set of scores.
        """
        is_list = isinstance(dorp,list)

        # If this is a plan panel, it only renders plans
        if (self.type == 'plan' or self.type == 'plan_summary') and \
            not isinstance(dorp,Plan):
            if is_list:
                if any(not isinstance(item,Plan) for item in dorp):
                    return ''
            else:
                return ''

        # Given a plan, it will render using the districts within the plan
        if self.type == 'district' and \
            not isinstance(dorp,District):
            if is_list:
                if any(not isinstance(item,District) for item in dorp):
                    return ''
            elif isinstance(dorp,Plan):
                dorp = dorp.get_districts_at_version(version or dorp.version, include_geom=True)
                is_list = True
            else:
                return ''

        # Render an item for each plan and plan score
        if self.type == 'plan' or self.type == 'plan_summary':
            if is_list:
                plans = dorp
            else:
                plans = [dorp]

            planscores = []

            # TODO: do we need a seperate per-panel description?
            description = ''
            
            for plan in plans:
                for function in self.score_functions.filter(is_planscore=True).order_by('name'):
                    description = function.description
                    planscores.append({
                        'plan':plan,
                        'name':function.name,
                        'label':function.label,
                        'description':function.description,
                        'score':ComputedPlanScore.compute(function,plan,format='html',version=version or plan.version),
                        'sort':ComputedPlanScore.compute(function,plan,format='sort',version=version or plan.version)
                    })

            if self.type == 'plan':
                planscores.sort(key=lambda x:x['sort'],reverse=not self.is_ascending)

            return render_to_string(self.template, {
                'settings':settings,
                'planscores':planscores,
                'title':self.title,
                'cssclass':self.cssclass,
                'position':self.position,
                'description':description,
                'planname': '' if len(plans) == 0 else plans[0].name,
                'context':context
            })

        # Render each district with multiple scores
        elif self.type == 'district':
            if is_list:
                districts = dorp
            else:
                districts = [dorp]

            districtscores = []
            functions = []
            for district in districts:
                districtscore = { 'district':district, 'scores':[] }

                for function in self.score_functions.filter(is_planscore=False):
                    if not function.label in functions:
                        functions.append(function.label)
                    score = ComputedDistrictScore.compute(function,district,format='html')
                    districtscore['scores'].append({
                        'district':district,
                        'name':function.name,
                        'label':function.label,
                        'description':function.description,
                        'score':score
                    })

                districtscores.append(districtscore)

            return render_to_string(self.template, {
                'districtscores':districtscores,
                'functions':functions,
                'title': self.title,
                'cssclass': self.cssclass
            })

class ValidationCriteria(models.Model):
    """
    Defines the required score functions to validate a legislative body
    """

    # The score function this criteria is for
    function = models.ForeignKey(ScoreFunction)

    # Name of this validation criteria
    name = models.CharField(max_length=50)

    # Description of this validation criteria
    description = models.TextField(blank=True)

    # The legislative body that this validation criteria is for
    legislative_body = models.ForeignKey(LegislativeBody)

    def __unicode__(self):
        return self.name

    class Meta:
        verbose_name_plural = "Validation criterion"


class ComputedDistrictScore(models.Model):
    """
    A score generated by a score function for a district that can be 
    saved for later.

    These computed scores do not store the version number, since each
    district has a unique version.
    """

    # The score function that computes this score
    function = models.ForeignKey(ScoreFunction)

    # The district that this score relates to
    district = models.ForeignKey(District)

    # The actual score value
    value = models.TextField()

    def __unicode__(self):
        name = ''
        if not self.district is None:
            if not self.district.plan is None:
                name = '%s / %s' % (self.district.name, self.district.plan.name,)
            else:
                name = self.district.name

        if not self.function is None:
            name = '%s / %s' % (self.function.name, name)
        else:
            name = 'None / %s' % name

        return name


    @staticmethod
    def compute(function,district,format='raw'):
        """
        Get the computed value. This method will leverage the cache when
        it is available, or it will populate the cache if it is not.

        If the cached score exists, it's value is not changed.

        If the cached score does not exist, this method will create it.

        Parameters:
            function -- A ScoreFunction to compute with
            district -- A District to compute on

        Returns:
            The cached value for the district.
        """
        created = False
        try:
            defaults = {'value':''}
            cache,created = ComputedDistrictScore.objects.get_or_create(function=function, district=district, defaults=defaults)

        except Exception as ex:
            print(traceback.format_exc())
            return None

        if created == True:
            score = function.score(district, format='raw')
            cache.value = cPickle.dumps(score)
            cache.save()
        else:
            try:
                score = cPickle.loads(str(cache.value))
            except:
                print('Failed to get cached value: %s\n' % traceback.format_exc())
                score = function.score(district, format='raw')

        if format != 'raw':
            calc = function.get_calculator()
            calc.result = score
            if format == 'html':
                return calc.html()
            elif format == 'json':
                return calc.json()
            elif format == 'sort':
                return calc.sortkey()
            else:
                # Unrecognized format!
                return None

        return score

    class Meta:
        unique_together = (('function','district'),)


class ComputedPlanScore(models.Model):
    """
    A score generated by a score function for a plan that can be saved
    for later.

    These computed scores contain version numbers, since a plan's version
    number is incremented each time, but scores need to exist for different
    plan version numbers, for history, etc.
    """

    # The score function that computes this score
    function = models.ForeignKey(ScoreFunction)

    # The plan that this score relates to
    plan = models.ForeignKey(Plan)

    # The version of the plan that this relates to
    version = models.PositiveIntegerField(default=0)

    # The actual score value
    value = models.TextField()

    @staticmethod
    def compute(function, plan, version=None, format='raw'):
        """
        Get the computed value. This method will leverage the cache when
        it is available, or it will populate the cache if it is not.

        If the cached score exists, it's value is not changed.

        If the cached score does not exist, this method will create it.

        Parameters:
            function -- A ScoreFunction to compute with
            plan -- A Plan to compute on
            version -- Optional; the version of the plan to compute.

        Returns:
            The cached value for the plan.
        """
        created = False
        try:
            defaults = {'value':''}
            cache,created = ComputedPlanScore.objects.get_or_create(function=function, plan=plan, version=version or plan.version, defaults=defaults)

        except Exception,e:
            print e
            return None

        if created:
            score = function.score(plan, format='raw', version=version or plan.version)
            cache.value = cPickle.dumps(score)
            cache.save()
        else:
            try:
                score = cPickle.loads(str(cache.value))
            except:
                score = function.score(plan, format='raw', version=version or plan.version)
                cache.value = cPickle.dumps(score)
                cache.save()

        if format != 'raw':
            calc = function.get_calculator()
            calc.result = score
            if format == 'html':
                return calc.html()
            elif format == 'json':
                return calc.json()
            elif format == 'sort':
                return calc.sortkey()
            else:
                # Unrecognized format!
                return None

        return score

    def __unicode__(self):
        name = ''
        if not self.plan is None:
            name = self.plan.name

        if not self.function is None:
            name = '%s / %s' % (self.function.name, name)
        else:
            name = 'None / %s' % name

        return name


class ContiguityOverride(models.Model):
    """
    Defines a relationship between two geounits in which special
    behavior needs to be applied when calculating contiguity.
    """

    # The geounit that is non-contiguous and needs an override applied
    override_geounit = models.ForeignKey(Geounit, related_name="override_geounit")

    # The geounit that the override_geounit is allowed to be considered
    # contiguous with, even in the absense of physical contiguity.
    connect_to_geounit = models.ForeignKey(Geounit, related_name="connect_to_geounit")

    # Manage the instances of this class with a geographically aware manager
    objects = models.GeoManager()

    def __unicode__(self):
        return '%s / %s' % (self.override_geounit.portable_id, self.connect_to_geounit.portable_id)

