#!/usr/bin/env python

from setuptools import setup

setup(name='mrs',
      packages=['mrs',
                'mrs.config',
                'mrs.config.default',
                'mrs.datasets',
                'mrs.db.models',
                'mrs.db.models.performance',
                'mrs.exceptions',
                'mrs.allocation',
                'mrs.tests',
                'mrs.messages',
                'mrs.timetable',
                'mrs.dispatching',
                'mrs.execution',
                'mrs.experiments',
                'mrs.experiments.config',
                'mrs.experiments.config.poses',
                'mrs.experiments.db.models',
                'mrs.experiments.results',
                'mrs.simulation',
                'mrs.performance',
                'mrs.utils'],
      install_requires=[
          'simpy',
          'planner@git+https://github.com/anenriquez/mrta_planner.git@master#egg=planner',
      ],
      version='0.2.0',
      description='Multi-Robot System (MRS) components for performing'
                  'Multi-Robot Task Allocation (MRTA) and executing'
                  'tasks with temporal constraints and uncertain '
                  'durations',
      author='Angela Enriquez Gomez',
      author_email='angela.enriquez@smail.inf.h-brs.de',
      package_dir={'': '.'}
      )
