# vi: ts=4 expandtab
#
#    Copyright (C) 2009-2010 Canonical Ltd.
#    Copyright (C) 2012 Hewlett-Packard Development Company, L.P.
#
#    Author: Scott Moser <scott.moser@canonical.com>
#    Author: Juerg Haefliger <juerg.haefliger@hp.com>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License version 3, as
#    published by the Free Software Foundation.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Puppet
------
**Summary:** install, configure and start puppet

This module handles puppet installation and configuration. If the ``puppet``
key does not exist in global configuration, no action will be taken. If a
config entry for ``puppet`` is present, then by default the latest version of
puppet will be installed. If ``install`` is set to ``false``, puppet will not
be installed. However, this may result in an error if puppet is not already
present on the system. The version of puppet to be installed can be specified
under ``version``, and defaults to ``none``, which selects the latest version
in the repos. If the ``puppet`` config key exists in the config archive, this
module will attempt to start puppet even if no installation was performed.

The module also provides keys for configuring the new puppet 4 paths and
installing the puppet package from the puppetlabs repositories:
https://docs.puppet.com/puppet/4.2/reference/whered_it_go.html
The keys are ``package_name``, ``conf_dir`` and ``ssl_dir``. If unset, their
values will default to ones that work with puppet 3.x and with distributions
that ship modified puppet 4.x that uses the old paths.

Puppet configuration can be specified under the ``conf`` key. The configuration
is specified as a dictionary which is converted into ``<key>=<value>`` format
and appended to ``puppet.conf`` under the ``[puppetd]`` section. The
``certname`` key supports string substitutions for ``%i`` and ``%f``,
corresponding to the instance id and fqdn of the machine respectively.
If ``ca_cert`` is present under ``conf``, it will not be written to
``puppet.conf``, but instead will be used as the puppermaster certificate.
It should be specified in pem format as a multi-line string (using the ``|``
yaml notation).

**Internal name:** ``cc_puppet``

**Module frequency:** per instance

**Supported distros:** all

**Config keys**::

    puppet:
        install: <true/false>
        version: <version>
        conf_dir: '/etc/puppet/'
        ssl_dir: '/var/lib/puppet/ssl'
        package_name: 'puppet'
        conf:
            server: "puppetmaster.example.org"
            certname: "%i.%f"
            ca_cert: |
                -------BEGIN CERTIFICATE-------
                <cert data>
                -------END CERTIFICATE-------
"""

from six import StringIO

import os
import socket

from cloudinit import helpers
from cloudinit import util

DEFAULT_PACKAGE_NAME = 'puppet'
DEFAULT_SSL_DIR = '/var/lib/puppet/ssl'
DEFAULT_CONF_DIR = '/etc/puppet'


class PuppetConstants(object):

    def __init__(self,
                 puppet_conf_dir,
                 puppet_ssl_dir,
                 log):
        self.conf_dir = puppet_conf_dir
        self.conf_path = os.path.join(puppet_conf_dir, "puppet.conf")
        self.ssl_dir = puppet_ssl_dir
        self.ssl_cert_dir = os.path.join(puppet_ssl_dir, "certs")
        self.ssl_cert_path = os.path.join(self.ssl_cert_dir,
                                          "ca.pem")


def _autostart_puppet(log):
    # Set puppet to automatically start
    if os.path.exists('/etc/default/puppet'):
        util.subp(['sed', '-i',
                   '-e', 's/^START=.*/START=yes/',
                   '/etc/default/puppet'], capture=False)
    elif os.path.exists('/bin/systemctl'):
        util.subp(['/bin/systemctl', 'enable', 'puppet.service'],
                  capture=False)
    elif os.path.exists('/sbin/chkconfig'):
        util.subp(['/sbin/chkconfig', 'puppet', 'on'], capture=False)
    else:
        log.warn(("Sorry we do not know how to enable"
                  " puppet services on this system"))


def handle(name, cfg, cloud, log, _args):
    # If there isn't a puppet key in the configuration don't do anything
    if 'puppet' not in cfg:
        log.debug(("Skipping module named %s,"
                   " no 'puppet' configuration found"), name)
        return

    puppet_cfg = cfg['puppet']
    # Start by installing the puppet package if necessary...
    install = util.get_cfg_option_bool(puppet_cfg, 'install', True)
    version = util.get_cfg_option_str(puppet_cfg, 'version', None)
    package_name = util.get_cfg_option_str(puppet_cfg,
                                           'package_name',
                                           DEFAULT_PACKAGE_NAME)
    conf_dir = util.get_cfg_option_str(puppet_cfg,
                                       'conf_dir',
                                       DEFAULT_CONF_DIR)
    ssl_dir = util.get_cfg_option_str(puppet_cfg,
                                      'ssl_dir',
                                      DEFAULT_SSL_DIR)

    p_constants = PuppetConstants(conf_dir,
                                  ssl_dir,
                                  log)
    if not install and version:
        log.warn(("Puppet install set false but version supplied,"
                  " doing nothing."))
    elif install:
        log.debug(("Attempting to install puppet %s,"),
                  version if version else 'latest')

        cloud.distro.install_packages((package_name, version))

    # ... and then update the puppet configuration
    if 'conf' in puppet_cfg:
        # Add all sections from the conf object to puppet.conf
        contents = util.load_file(p_constants.conf_path)
        # Create object for reading puppet.conf values
        puppet_config = helpers.DefaultingConfigParser()
        # Read puppet.conf values from original file in order to be able to
        # mix the rest up. First clean them up
        # (TODO(harlowja) is this really needed??)
        cleaned_lines = [i.lstrip() for i in contents.splitlines()]
        cleaned_contents = '\n'.join(cleaned_lines)
        puppet_config.readfp(StringIO(cleaned_contents),
                             filename=p_constants.conf_path)
        for (cfg_name, cfg) in puppet_cfg['conf'].items():
            # Cert configuration is a special case
            # Dump the puppet master ca certificate in the correct place
            if cfg_name == 'ca_cert':
                # Puppet ssl sub-directory isn't created yet
                # Create it with the proper permissions and ownership
                util.ensure_dir(p_constants.ssl_dir, 0o771)
                util.chownbyname(p_constants.ssl_dir, 'puppet', 'root')
                util.ensure_dir(p_constants.ssl_cert_dir)
                util.chownbyname(p_constants.ssl_cert_dir, 'puppet', 'root')
                util.write_file(p_constants.ssl_cert_path, cfg)
                util.chownbyname(p_constants.ssl_cert_path, 'puppet', 'root')
            else:
                # Iterate through the config items, we'll use ConfigParser.set
                # to overwrite or create new items as needed
                for (o, v) in cfg.items():
                    if o == 'certname':
                        # Expand %f as the fqdn
                        # TODO(harlowja) should this use the cloud fqdn??
                        v = v.replace("%f", socket.getfqdn())
                        # Expand %i as the instance id
                        v = v.replace("%i", cloud.get_instance_id())
                        # certname needs to be downcased
                        v = v.lower()
                    puppet_config.set(cfg_name, o, v)
            # We got all our config as wanted we'll rename
            # the previous puppet.conf and create our new one
            util.rename(p_constants.conf_path, "%s.old"
                        % (p_constants.conf_path))
            util.write_file(p_constants.conf_path, puppet_config.stringify())

    # Set it up so it autostarts
    _autostart_puppet(log)

    # Start puppetd
    util.subp(['service', 'puppet', 'start'], capture=False)
