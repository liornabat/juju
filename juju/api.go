// Copyright 2013 Canonical Ltd.
// Licensed under the AGPLv3, see LICENCE file for details.

package juju

import (
	"fmt"
	"time"

	"launchpad.net/loggo"

	"launchpad.net/juju-core/environs"
	"launchpad.net/juju-core/environs/config"
	"launchpad.net/juju-core/environs/configstore"
	"launchpad.net/juju-core/errors"
	"launchpad.net/juju-core/names"
	"launchpad.net/juju-core/state/api"
	"launchpad.net/juju-core/state/api/keymanager"
)

var logger = loggo.GetLogger("juju")

// The following are variables so that they can be
// changed by tests.
var (
	apiOpen              = api.Open
	apiClose             = (*api.State).Close
	providerConnectDelay = 2 * time.Second
)

// APIConn holds a connection to a juju environment and its
// associated state through its API interface.
type APIConn struct {
	Environ environs.Environ
	State   *api.State
}

var errAborted = fmt.Errorf("aborted")

func prepareAPIInfo(environ environs.Environ) (*api.Info, error) {
	_, info, err := environ.StateInfo()
	if err != nil {
		return nil, err
	}
	info.Tag = "user-admin"
	password := environ.Config().AdminSecret()
	if password == "" {
		return nil, fmt.Errorf("cannot connect without admin-secret")
	}
	info.Password = password
	return info, nil
}

// NewAPIConn returns a new Conn that uses the
// given environment. The environment must have already
// been bootstrapped.
func NewAPIConn(environ environs.Environ, dialOpts api.DialOpts) (*APIConn, error) {
	info, err := prepareAPIInfo(environ)
	if err != nil {
		return nil, err
	}

	st, err := apiOpen(info, dialOpts)
	// TODO(rog): handle errUnauthorized when the API handles passwords.
	if err != nil {
		return nil, err
	}
	return &APIConn{
		Environ: environ,
		State:   st,
	}, nil
}

// Close terminates the connection to the environment and releases
// any associated resources.
func (c *APIConn) Close() error {
	return apiClose(c.State)
}

// NewAPIClientFromName returns an api.Client connected to the API Server for
// the named environment. If envName is "", the default environment
// will be used.
func NewAPIClientFromName(envName string) (*api.Client, error) {
	st, err := newAPIClient(envName)
	if err != nil {
		return nil, err
	}
	return st.Client(), nil
}

// NewKeyManagerClient returns an api.keymanager.Client connected to the API Server for
// the named environment. If envName is "", the default environment will be used.
func NewKeyManagerClient(envName string) (*keymanager.Client, error) {
	st, err := newAPIClient(envName)
	if err != nil {
		return nil, err
	}
	return keymanager.NewClient(st), nil
}

func newAPIClient(envName string) (*api.State, error) {
	store, err := configstore.NewDisk(config.JujuHome())
	if err != nil {
		return nil, err
	}
	return newAPIFromName(envName, store)
}

// newAPIFromName implements the bulk of NewAPIClientFromName
// but is separate for testing purposes.
func newAPIFromName(envName string, store configstore.Storage) (*api.State, error) {
	// Try to read the default environment configuration file.
	// If it doesn't exist, we carry on in case
	// there's some environment info for that environment.
	// This enables people to copy environment files
	// into their .juju/environments directory and have
	// them be directly useful with no further configuration changes.
	envs, err := environs.ReadEnvirons("")
	if err == nil {
		if envName == "" {
			envName = envs.Default
		}
		if envName == "" {
			return nil, fmt.Errorf("no default environment found")
		}
	} else if !environs.IsNoEnv(err) {
		return nil, err
	}

	// Try to connect to the API concurrently using two different
	// possible sources of truth for the API endpoint. Our
	// preference is for the API endpoint cached in the API info,
	// because we know that without needing to access any remote
	// provider. However, the addresses stored there may no longer
	// be current (and the network connection may take a very long
	// time to time out) so we also try to connect using information
	// found from the provider. We only start to make that
	// connection after some suitable delay, so that in the
	// hopefully usual case, we will make the connection to the API
	// and never hit the provider. By preference we use provider
	// attributes from the config store, but for backward
	// compatibility reasons, we fall back to information from
	// ReadEnvirons if that does not exist.

	stop := make(chan struct{})
	defer close(stop)

	info, err := store.ReadInfo(envName)
	if err != nil && !errors.IsNotFoundError(err) {
		return nil, err
	}
	var infoResult <-chan apiOpenResult
	if info != nil {
		infoResult = apiInfoConnect(store, info, stop)
	}
	delay := providerConnectDelay
	var cfgResult <-chan apiOpenResult
	if infoResult == nil {
		// There's no environment info, so no need to
		// wait for the info connection.
		logger.Infof("no cached API connection settings found")
		delay = 0
		cfgResult = apiConfigConnect(info, envs, envName, stop, delay)
	} else {
		logger.Infof("using cached API connection settings")
	}

	if infoResult == nil && cfgResult == nil {
		return nil, errors.NotFoundf("environment %q", envName)
	}
	var (
		st            *api.State
		connectedInfo *api.Info
		infoErr       error
		cfgErr        error
	)
	for st == nil && (infoResult != nil || cfgResult != nil) {
		select {
		case r := <-infoResult:
			st = r.st
			infoErr = r.err
			infoResult = nil
			connectedInfo = r.info
		case r := <-cfgResult:
			st = r.st
			cfgErr = r.err
			cfgResult = nil
			connectedInfo = r.info
		}
	}
	if st != nil {
		// One potential issue: there may still be a lingering
		// API connection, which will use resources until it
		// finally succeeds or fails. Unless we are making hundreds
		// of API connections, this is unlikely to be a problem.

		// Cache the successful connection info for future use.
		info.SetAPIEndpoint(configstore.APIEndpoint{
			Addresses: connectedInfo.Addrs,
			CACert:    string(connectedInfo.CACert),
		})
		_, username, err := names.ParseTag(connectedInfo.Tag, names.UserTagKind)
		if err != nil {
			logger.Errorf("invalid API user tag: %v", err)
			st.Close()
			return nil, err
		}
		info.SetAPICredentials(configstore.APICredentials{
			User:     username,
			Password: connectedInfo.Password,
		})
		if err := info.Write(); err != nil {
			// Not fatal, just the cache won't be updated.
			logger.Warningf("cannot cache API connection settings: %v", err)
		}
		logger.Infof("updated API connection settings cache")
		return st, nil
	}
	if cfgErr != nil {
		// Return the error from the configuration lookup if we
		// have one, because that information should be most current.
		logger.Warningf("discarding API open error: %v", infoErr)
		return nil, cfgErr
	}
	return nil, infoErr
}

type apiOpenResult struct {
	st   *api.State
	info *api.Info
	err  error
}

// apiInfoConnect looks for endpoint on the given environment and
// tries to connect to it, sending the result on the returned channel.
func apiInfoConnect(store configstore.Storage, info configstore.EnvironInfo, stop <-chan struct{}) <-chan apiOpenResult {
	resultc := make(chan apiOpenResult)
	endpoint := info.APIEndpoint()

	if info == nil || len(endpoint.Addresses) == 0 {
		return nil
	}
	go func() {
		logger.Infof("connecting to API addresses: %v", endpoint.Addresses)
		apiInfo := &api.Info{
			Addrs:    endpoint.Addresses,
			CACert:   []byte(endpoint.CACert),
			Tag:      names.UserTag(info.APICredentials().User),
			Password: info.APICredentials().Password,
		}
		st, err := apiOpen(apiInfo, api.DefaultDialOpts())
		if err != nil {
			logger.Infof("failed to connect to API addresses: %v, %v", endpoint.Addresses, err)
		}
		sendAPIOpenResult(resultc, stop, st, apiInfo, err)
	}()
	return resultc
}

func sendAPIOpenResult(resultc chan apiOpenResult, stop <-chan struct{}, st *api.State, info *api.Info, err error) {
	select {
	case <-stop:
		if err != nil {
			if err != errAborted {
				logger.Warningf("discarding stale API open error: %v", err)
			}
		} else {
			apiClose(st)
		}
	case resultc <- apiOpenResult{st, info, err}:
	}
}

// apiConfigConnect looks for configuration info on the given environment,
// and tries to use an Environ constructed from that to connect to
// its endpoint. It only starts the attempt after the given delay,
// to allow the faster apiInfoConnect to hopefully succeed first.
// It returns nil if there was no configuration information found.
func apiConfigConnect(info configstore.EnvironInfo, envs *environs.Environs, envName string, stop <-chan struct{}, delay time.Duration) <-chan apiOpenResult {
	resultc := make(chan apiOpenResult)
	var cfg *config.Config
	var err error
	if info != nil && len(info.BootstrapConfig()) > 0 {
		cfg, err = config.New(config.NoDefaults, info.BootstrapConfig())
	} else if envs != nil {
		cfg, err = envs.Config(envName)
		if errors.IsNotFoundError(err) {
			return nil
		}
	} else {
		return nil
	}
	connect := func() (*api.State, *api.Info, error) {
		if err != nil {
			return nil, nil, err
		}
		select {
		case <-time.After(delay):
		case <-stop:
			return nil, nil, errAborted
		}
		environ, err := environs.New(cfg)
		if err != nil {
			return nil, nil, err
		}
		apiInfo, err := prepareAPIInfo(environ)
		if err != nil {
			return nil, nil, err
		}
		apiConn, err := NewAPIConn(environ, api.DefaultDialOpts())
		if err != nil {
			return nil, nil, err
		}
		return apiConn.State, apiInfo, nil
	}
	go func() {
		st, apiInfo, err := connect()
		sendAPIOpenResult(resultc, stop, st, apiInfo, err)
	}()
	return resultc
}
