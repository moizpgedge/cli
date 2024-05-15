# This testcase enables the the two primary auto ddl gucs 
# spock.enable_ddl_replication AND spock.include_ddl_repset
# on node 2 and performs a reload.
#
use strict;
use warnings;
use File::Which;
use IPC::Cmd qw(run);
use Try::Tiny;
use JSON;
use lib './t/lib';
use contains;
use edge;

# Our parameters are:

my $homedir2="$ENV{EDGE_CLUSTER_DIR}/n2/pgedge";
my $host = $ENV{EDGE_HOST};
my $port = $ENV{'EDGE_START_PORT'} + 1;
my $database = $ENV{EDGE_DB};
print("The home directory of node 2 is $homedir2\n");

# command to disable spock.enable_ddl_replication 
my $enableDDLRep = qq(alter system set spock.enable_ddl_replication = off);
# command to disable spock.include_ddl_repset
my $includeDDLRep = qq(alter system set spock.include_ddl_repset = off);
# command to disable spock.allow_ddl_from_functions
my $allowDDLFunc = qq(alter system set spock.allow_ddl_from_functions = off);
# execute the two alter syste commands followed by a server reload
# all commands cannot be combined in a single -c switch
my $cmd1 = qq($homedir2/$ENV{EDGE_COMPONENT}/bin/psql -t -h $host -p $port -d $database -c "$enableDDLRep" -c "$includeDDLRep" -c "$allowDDLFunc" -c "select pg_reload_conf()");
print("cmd1 = $cmd1\n");
my $stdout_buf= (run_command_and_exit_iferr ($cmd1))[3];
print("stdout_buf = @$stdout_buf\n");
sleep(0.5); #for the reload to be ready

# Validate the two gucs are OFF
my $cmd2 = qq($homedir2/$ENV{EDGE_COMPONENT}/bin/psql -t -h $host -p $port -d $database -c "show spock.enable_ddl_replication" -c "show spock.include_ddl_repset" -c "show spock.allow_ddl_from_functions");
print("cmd2 = $cmd2\n");
my $stdout_buf1= (run_command_and_exit_iferr ($cmd2))[3];
#combining a multi line output into a single line to ensure consistent comparisons to avoid random \n messing up comparisons
@$stdout_buf1 = join(' ', map { sanitize_and_combine_multiline_stdout($_) } @$stdout_buf1);
print("stdout_buf1 = @$stdout_buf1\n");

if (contains($stdout_buf1->[0], "off off off"))
{
    print("AutoDDL Gucs spock.enable_ddl_replication, spock.include_ddl_repset and spock.allow_ddl_from_functions are now off");
    exit(0);
}
else
{
    print("One or more of AutoDDL GUCs could not be disabled. Exiting with failure");
    exit(1);
}